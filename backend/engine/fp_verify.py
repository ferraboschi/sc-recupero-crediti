"""Verifica di allineamento fatture ↔ FatturaPro per UN cliente (pulsante
"Verifica con FatturaPro" nella scheda): confronto puro, testabile, fra le
fatture della piattaforma e i documenti trovati su FatturaPro per quel
destinatario. Le CORREZIONI proposte sono esplicite e le applica l'operatore.
"""
from typing import Any, Dict, List, Optional

from backend.engine.sdi import SDI_FINAL_OK, SDI_LABELS, sdi_state_from_label

VERDICT_LABELS = {
    "ok": "Allineata",
    "mancante": "Su FatturaPro ma non in piattaforma",
    "inesistente": "In piattaforma ma non su FatturaPro",
    "non_valida": "Non trasmessa / scartata su FatturaPro",
    "numero_riassegnato": "Stesso numero, documento diverso",
    "importo_diverso": "Importo diverso",
    "pagata_su_fatturapro": "Saldata su FatturaPro, aperta in piattaforma",
    "riaperta_su_fatturapro": "Aperta su FatturaPro, pagata in piattaforma",
    "da_riattivare": "Valida su FatturaPro, annullata in piattaforma",
    "pagata_non_tracciata": "Saldata su FatturaPro, mai importata",
}

# Correzione proposta per ciascun verdetto (None = nessuna azione automatica).
VERDICT_FIX = {
    "mancante": "import",
    "inesistente": "void",
    "non_valida": "void",
    "numero_riassegnato": "void",
    "importo_diverso": "update_amount",
    "pagata_su_fatturapro": "mark_paid",
    "riaperta_su_fatturapro": "reopen",
    "da_riattivare": "reactivate",
}


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


def compare_documents(platform_invoices: List[Any], fp_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Confronta le fatture della piattaforma (oggetti Invoice, source
    fatturapro) con le righe FatturaPro dello stesso destinatario.

    Ritorna {"rows": [...], "summary": {verdetto: n}}: una riga per numero
    fattura, con i dati dei due lati, il verdetto e la correzione proposta.
    """
    by_num_fp: Dict[str, Dict[str, Any]] = {}
    for r in fp_rows:
        num = (r.get("invoice_number") or "").strip()
        if num:
            by_num_fp[num] = r
    by_num_pl: Dict[str, Any] = {}
    for inv in platform_invoices:
        by_num_pl[(inv.invoice_number or "").strip()] = inv

    rows: List[Dict[str, Any]] = []
    numbers = sorted(set(by_num_fp) | set(by_num_pl), reverse=True)
    for num in numbers:
        fp = by_num_fp.get(num)
        pl = by_num_pl.get(num)
        fp_state = fp_state_of(fp) if fp else None
        fp_valid = fp is not None and fp_state in SDI_FINAL_OK
        fp_saldo = float(fp.get("balance") or 0) if fp else None
        fp_total = float(fp.get("total") or 0) if fp else None
        verdict = "ok"
        if fp is None:
            verdict = "inesistente" if pl is not None and pl.status != "void" else "ok"
        elif pl is None:
            if fp_valid and (fp_saldo or 0) > 0:
                verdict = "mancante"
            elif fp_valid:
                verdict = "pagata_non_tracciata"
            else:
                verdict = "ok"  # bozza/in elaborazione/scartata: giusto che non ci sia
        else:
            if not fp_valid:
                verdict = "non_valida" if pl.status != "void" else "ok"
            elif pl.status == "void":
                verdict = "da_riattivare"
            elif (fp.get("doc_id") and pl.source_id
                  and str(fp.get("doc_id")) != str(pl.source_id)):
                verdict = "numero_riassegnato"
            elif pl.status == "paid" and (fp_saldo or 0) > 0:
                verdict = "riaperta_su_fatturapro"
            elif pl.status != "paid" and (fp_saldo or 0) == 0:
                verdict = "pagata_su_fatturapro"
            elif fp_total is not None and abs(float(pl.amount or 0) - fp_total) > 0.005:
                verdict = "importo_diverso"
        if verdict == "ok" and pl is None:
            continue  # documento FatturaPro non pertinente: niente da mostrare
        rows.append({
            "invoice_number": num,
            "verdict": verdict,
            "verdict_label": VERDICT_LABELS.get(verdict, verdict),
            "fix": VERDICT_FIX.get(verdict),
            "fatturapro": None if fp is None else {
                "doc_id": fp.get("doc_id"),
                "date": fp.get("date").isoformat() if fp.get("date") else None,
                "total": fp_total,
                "balance": fp_saldo,
                "state": fp_state,
                "state_label": fp.get("fp_state_label") or SDI_LABELS.get(fp_state or "", None),
                "customer_name": fp.get("customer_name"),
            },
            "platform": None if pl is None else {
                "id": pl.id,
                "status": pl.status,
                "amount": float(pl.amount or 0),
                "amount_due": float(pl.amount_due or 0),
                "source_id": pl.source_id,
                "sdi_state": getattr(pl, "sdi_state", None),
                "issue_date": pl.issue_date.isoformat() if pl.issue_date else None,
                "due_date": pl.due_date.isoformat() if pl.due_date else None,
            },
        })
    summary: Dict[str, int] = {}
    for r in rows:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    return {"rows": rows, "summary": summary}
