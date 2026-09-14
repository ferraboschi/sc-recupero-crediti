"""Verifica di allineamento fatture ↔ FatturaPro per UN cliente (pulsante
"Verifica con FatturaPro" nella scheda): confronto puro, testabile, fra le
fatture della piattaforma e i documenti trovati su FatturaPro per quel
destinatario. Le CORREZIONI proposte sono esplicite e le applica l'operatore.
"""
import re
from typing import Any, Dict, List, Optional

from backend.engine.sdi import SDI_FINAL_OK, SDI_LABELS, sdi_state_from_label
from backend.connectors.fatturapro import doc_key

VERDICT_LABELS = {
    "ok": "Allineata",
    "mancante": "Su FatturaPro ma non in piattaforma",
    "inesistente": "In piattaforma ma non su FatturaPro",
    "non_verificabile": "Non trovata su FatturaPro (lista incompleta)",
    "non_valida": "Non ancora consegnata su FatturaPro (bozza, in elaborazione o scartata)",
    "numero_riassegnato": "Stesso numero, documento diverso",
    "importo_diverso": "Importo diverso",
    "pagata_su_fatturapro": "Saldata su FatturaPro, aperta in piattaforma",
    "riaperta_su_fatturapro": "Aperta su FatturaPro, pagata in piattaforma",
    "da_riattivare": "Valida su FatturaPro, annullata in piattaforma",
    "pagata_non_tracciata": "Saldata su FatturaPro, mai importata",
    "duplicato": "Doppione in piattaforma (documento diverso dallo stesso numero)",
    "rinumerata": "Rinumerata da FatturaPro (stesso documento, numero nuovo)",
}

# Correzione proposta per ciascun verdetto (None = nessuna azione automatica).
# 'replace' = annulla la riga vecchia (documento sparito) E importa il nuovo.
VERDICT_FIX = {
    "mancante": "import",
    "inesistente": "void",
    "non_valida": "void",
    "numero_riassegnato": "replace",
    "importo_diverso": "update_amount",
    "pagata_su_fatturapro": "mark_paid",
    "riaperta_su_fatturapro": "reopen",
    "da_riattivare": "reactivate",
    "duplicato": "void",
    "rinumerata": "renumber",
}

# Correzioni che si applicano senza confronto con lo stato attuale (non
# distruttive): pre-selezionabili dalla UI. Le altre le spunta l'operatore.
SAFE_FIXES = ("update_amount", "renumber")


def fp_state_of(row: Dict[str, Any]) -> Optional[str]:
    """Stato SDI di una riga FatturaPro: dalla colonna Stato se c'è, altrimenti
    dalla firma di riga (draft/sent/notified→consegnata presunta)."""
    st = sdi_state_from_label(row.get("fp_state_label"))
    if st:
        return st
    sig = row.get("fp_signature")
    if sig == "draft":
        return "draft"
    if sig == "sent":
        return "sent"
    if sig == "notified":
        return "consegnata"
    return None


def _natural_key(num: str):
    """Ordina per anno e numero progressivo (non per stringa: '999' > '1000')."""
    digits = [int(x) for x in re.findall(r"\d+", num or "")]
    return (digits[0] if digits else 0, digits[1] if len(digits) > 1 else 0, num)


def compare_documents(platform_invoices: List[Any], fp_rows: List[Dict[str, Any]],
                      complete: bool = True) -> Dict[str, Any]:
    """Confronta le fatture della piattaforma (oggetti Invoice, source
    fatturapro) con le righe FatturaPro dello stesso destinatario.

    Le righe ANNULLATE (void) della piattaforma non sono crediti: contano solo
    per proporre la riattivazione del loro stesso documento (doc_id uguale).
    Con `complete=False` (ricerca FatturaPro incompleta o fallita) l'assenza
    di un documento NON è una prova: niente verdetto 'inesistente', solo
    'non_verificabile' senza correzione.

    Ritorna {"rows": [...], "summary": {verdetto: n}}: una riga per numero
    fattura, con i dati dei due lati, il verdetto e la correzione proposta.
    """
    # Le due liste di FatturaPro possono rendere il numero con forme
    # diverse: si confronta per chiave canonica (doc_key, anno incluso).
    by_num_fp: Dict[str, Dict[str, Any]] = {}
    label_of: Dict[str, str] = {}
    for r in fp_rows:
        raw = (r.get("invoice_number") or "").strip()
        num = doc_key(raw) if raw else ""
        if num:
            by_num_fp[num] = r
            label_of.setdefault(num, raw)
    # IDENTITÀ = doc_id: una riga della piattaforma il cui doc_id è su
    # FatturaPro sotto un ALTRO numero è lo stesso documento rinumerato →
    # si confronta col numero nuovo e si propone 'rinumerata'.
    fp_num_by_doc: Dict[str, str] = {}
    for r in fp_rows:
        if r.get("doc_id") and (r.get("invoice_number") or "").strip():
            fp_num_by_doc[str(r["doc_id"])] = doc_key((r.get("invoice_number") or "").strip())
    renumber_from: Dict[int, str] = {}
    active_pl: Dict[str, Any] = {}
    extra_active: List[Any] = []  # doppioni attivi (stesso numero, documento diverso)
    void_pl: Dict[str, List[Any]] = {}
    for inv in platform_invoices:
        num = doc_key((inv.invoice_number or "").strip())
        fp_num = fp_num_by_doc.get(str(inv.source_id or ""))
        if fp_num and fp_num != num:
            renumber_from[inv.id] = (inv.invoice_number or "").strip()
            num = fp_num
        label_of.setdefault(num, (inv.invoice_number or "").strip())
        if inv.status == "void":
            void_pl.setdefault(num, []).append(inv)
            continue
        fp = by_num_fp.get(num)
        if num in active_pl:
            # Due attive con lo stesso numero: "la" riga è quella il cui
            # doc_id coincide con FatturaPro; l'altra è un doppione.
            cur = active_pl[num]
            cur_match = bool(fp and cur.source_id and str(cur.source_id) == str(fp.get("doc_id")))
            new_match = bool(fp and inv.source_id and str(inv.source_id) == str(fp.get("doc_id")))
            if new_match and not cur_match:
                extra_active.append(cur)
                active_pl[num] = inv
            else:
                extra_active.append(inv)
            continue
        active_pl[num] = inv

    rows: List[Dict[str, Any]] = []
    numbers = sorted(set(by_num_fp) | set(active_pl) | set(void_pl), key=_natural_key, reverse=True)
    for num in numbers:
        fp = by_num_fp.get(num)
        pl = active_pl.get(num)
        voids = void_pl.get(num, [])
        fp_state = fp_state_of(fp) if fp else None
        fp_valid = fp is not None and fp_state in SDI_FINAL_OK
        fp_saldo = float(fp.get("balance") or 0) if fp else None
        fp_total = float(fp.get("total") or 0) if fp else None
        verdict = "ok"
        shown = pl
        if fp is None:
            if pl is None:
                continue  # solo annullate: nulla da fare
            verdict = "inesistente" if complete else "non_verificabile"
        elif not fp_valid:
            verdict = "non_valida" if pl is not None else "ok"
            if pl is None:
                continue
        elif pl is None:
            same_doc = [v for v in voids if fp.get("doc_id") and v.source_id and str(v.source_id) == str(fp.get("doc_id"))]
            if same_doc:
                verdict = "da_riattivare"
                shown = same_doc[0]
            elif (fp_saldo or 0) > 0:
                verdict = "mancante"
            else:
                verdict = "pagata_non_tracciata"
        else:
            if fp.get("doc_id") and pl.source_id and str(fp.get("doc_id")) != str(pl.source_id):
                verdict = "numero_riassegnato"
            elif pl.status == "paid" and (fp_saldo or 0) > 0:
                verdict = "riaperta_su_fatturapro"
            elif pl.status != "paid" and (fp_saldo or 0) == 0:
                verdict = "pagata_su_fatturapro"
            elif fp_total is not None and abs(float(pl.amount or 0) - fp_total) > 0.005:
                verdict = "importo_diverso"
        if shown is not None and shown.id in renumber_from and verdict == "ok":
            verdict = "rinumerata"
        fix = VERDICT_FIX.get(verdict)
        rows.append({
            "invoice_number": (fp.get("invoice_number") or "").strip() if fp else label_of.get(num, num),
            "renumber_from": renumber_from.get(shown.id) if shown is not None else None,
            "key": f"{num}#{shown.id if shown is not None else 'fp'}",
            "verdict": verdict,
            "verdict_label": VERDICT_LABELS.get(verdict, verdict),
            "fix": fix,
            "fix_safe": fix in SAFE_FIXES,
            "fatturapro": None if fp is None else {
                "doc_id": fp.get("doc_id"),
                "date": fp.get("date").isoformat() if fp.get("date") else None,
                "total": fp_total,
                "balance": fp_saldo,
                "state": fp_state,
                "state_label": fp.get("fp_state_label") or SDI_LABELS.get(fp_state or "", None),
                "customer_name": fp.get("customer_name"),
            },
            "platform": None if shown is None else {
                "id": shown.id,
                "status": shown.status,
                "amount": float(shown.amount or 0),
                "amount_due": float(shown.amount_due or 0),
                "source_id": shown.source_id,
                "sdi_state": getattr(shown, "sdi_state", None),
                "issue_date": shown.issue_date.isoformat() if shown.issue_date else None,
                "due_date": shown.due_date.isoformat() if shown.due_date else None,
            },
        })
    for dup in extra_active:
        rows.append({
            "invoice_number": (dup.invoice_number or "").strip(),
            "key": f"{doc_key((dup.invoice_number or '').strip())}#{dup.id}",
            "verdict": "duplicato",
            "verdict_label": VERDICT_LABELS["duplicato"],
            "fix": "void",
            "fix_safe": False,
            "fatturapro": None,
            "platform": {
                "id": dup.id, "status": dup.status, "amount": float(dup.amount or 0),
                "amount_due": float(dup.amount_due or 0), "source_id": dup.source_id,
                "sdi_state": getattr(dup, "sdi_state", None),
                "issue_date": dup.issue_date.isoformat() if dup.issue_date else None,
                "due_date": dup.due_date.isoformat() if dup.due_date else None,
            },
        })
    summary: Dict[str, int] = {}
    for r in rows:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    return {"rows": rows, "summary": summary, "complete": complete}
