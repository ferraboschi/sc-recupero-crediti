"""Sezione Avvocato: candidati alla pratica legale + dossier documentale.

Candidato = debito scaduto NETTO > soglia (default €1.500) E entrambi i
solleciti fatti (contact_count >= 2), attivo / non escluso / non fuso, non
ancora passato all'avvocato. La lista è auto-calcolata dai dati.

Regola di consegna (decisa dall'owner): compare appena maturato il criterio,
ma la lista mostra i GIORNI DALL'ULTIMO SOLLECITO così l'operatore non passa al
legale chi è stato appena sollecitato (soglia visiva LAWYER_GRACE_DAYS). Il
passaggio all'avvocato è manuale, mai automatico.

Consegna = DOWNLOAD del pacchetto (niente invio email): un Dossier PDF (fatture
con date + timeline attività con date + totale) più i PDF delle singole
fatture, in uno ZIP per cliente; oppure un unico ZIP con una cartella per
candidato ("prepara tutti").
"""
import io
import os
import re
import zipfile
import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from backend.database import (
    get_session, Customer, Invoice, RecoveryAction, RecoveryActionInvoice,
)
from backend.engine.overdue import overdue_clause, in_incasso_clause
from backend.engine.cases import get_open_case
from backend.engine.action_invoices import (
    per_invoice_sollecito_stats, per_invoice_actions, set_action_invoices,
    delivered_invoice_ids,
)
from backend.api.recovery import _build_invoice_pdf

logger = logging.getLogger(__name__)
router = APIRouter()

# Soglia debito e grazia configurabili (fallback ai valori decisi dall'owner).
LAWYER_MIN_DEBT = float(os.getenv("LAWYER_MIN_DEBT", "1500"))
# Soglia VISIVA "pronto vs sollecitato di recente": stessa grazia (14gg) che il
# ciclo già usa fra 2° sollecito e avvocato.
LAWYER_GRACE_DAYS = int(os.getenv("LAWYER_GRACE_DAYS", "14"))

CONTACT_TYPES = ("first_contact", "second_contact")

ACTION_LABELS = {
    "first_contact": "1° sollecito",
    "second_contact": "2° sollecito",
    "lawyer": "Passaggio all'avvocato",
    "wait": "Attesa",
    "note": "Nota",
    "archive": "Archiviazione",
}
CHANNEL_LABELS = {
    "whatsapp_copy": "WhatsApp (messaggio copiato)",
    "whatsapp_link": "WhatsApp (link)",
    "phone": "Telefono",
    "email": "Email",
}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip()) or "cliente"


def _lat1(s) -> str:
    """Rende una stringa sicura per i font core di fpdf (latin-1): i caratteri
    fuori latin-1 (virgolette tipografiche, trattino lungo, ideogrammi) sono
    sostituiti invece di far crashare la generazione del PDF."""
    return str(s if s is not None else "").encode("latin-1", "replace").decode("latin-1")


class HandoverBody(BaseModel):
    # Fatture da consegnare all'avvocato: le SCEGLIE l'operatore (decisione
    # owner). Vuoto = tutte le scadute non ancora consegnate (legacy).
    invoice_ids: List[int] = []


def _parse_ids(raw: Optional[str]) -> Optional[List[int]]:
    """'12,15,20' → [12, 15, 20]; None/vuoto → None (= nessun filtro)."""
    if raw is None or not raw.strip():
        return None
    try:
        return sorted({int(x) for x in raw.split(",") if x.strip()})
    except ValueError:
        raise HTTPException(status_code=400, detail="invoice_ids non validi")


def _debt_clause():
    """Debito PERSEGUIBILE (= is_overdue_unpaid in SQL): scaduto, non pagato,
    NON contestato. Le contestate non vanno né nel totale che decide la soglia
    né nel dossier consegnato all'avvocato."""
    return overdue_clause() & (Invoice.status != "disputed") & ~in_incasso_clause()


def _lawyer_actions(session: Session, case):
    """Azioni 'lawyer' COMPLETATE non annullate della pratica (= consegne)."""
    return session.query(RecoveryAction).filter(
        RecoveryAction.case_id == case.id,
        RecoveryAction.action_type == "lawyer",
        RecoveryAction.completed_at.isnot(None),
        RecoveryAction.cancelled.isnot(True),
    ).all()


def _delivered_invoice_ids(session: Session, case, overdue_invoices) -> set:
    """Vedi engine.action_invoices.delivered_invoice_ids (unica definizione)."""
    return delivered_invoice_ids(session, case, overdue_invoices)


def _case_delivered_to_lawyer(session: Session, case) -> bool:
    """Compat: TUTTE le scadute del ciclo aperto sono consegnate."""
    overdue = _overdue_invoices(session, case.customer_id)
    if not overdue:
        return False
    return {i.id for i in overdue} <= _delivered_invoice_ids(session, case, overdue)


def _overdue_invoices(session: Session, customer_id: int):
    """Le fatture scadute non pagate del cliente (stessa definizione ovunque)."""
    return (
        session.query(Invoice)
        .filter(Invoice.customer_id == customer_id, _debt_clause())
        .order_by(Invoice.due_date.asc())
        .all()
    )


def _candidate_rows(session: Session):
    """Calcola i candidati alla pratica legale.

    Ritorna una lista di dict ordinata per scaduto desc. UNA aggregazione per
    lo scaduto; contatti e ultimo sollecito si calcolano — SCOPED alla pratica
    aperta — solo sui pochi clienti sopra soglia (non un N+1 sull'anagrafica).
    """
    # 1. Debito PERSEGUIBILE per cliente (esclude pagate/contestate).
    stats = dict(
        (r[0], {"total_overdue": float(r[1] or 0), "overdue_count": int(r[2] or 0)})
        for r in session.query(
            Invoice.customer_id,
            func.sum(case((_debt_clause(), Invoice.amount_due), else_=0)),
            func.sum(case((_debt_clause(), 1), else_=0)),
        )
        .filter(Invoice.status != "paid", Invoice.customer_id.isnot(None))
        .group_by(Invoice.customer_id)
        .all()
    )

    # 2. Clienti sopra soglia, attivi (excluded IS NOT TRUE gestisce anche i
    #    NULL legacy), non fusi.
    over_ids = [cid for cid, s in stats.items() if s["total_overdue"] > LAWYER_MIN_DEBT]
    if not over_ids:
        return []
    customers = (
        session.query(Customer)
        .filter(
            Customer.id.in_(over_ids),
            Customer.merged_into.is_(None),
            Customer.excluded.isnot(True),
        )
        .all()
    )

    today = date.today()
    rows = []
    for cust in customers:
        case_obj = get_open_case(session, cust.id)
        if case_obj is None:
            continue
        # "Consegnato" = la pratica APERTA ha un'azione 'lawyer' COMPLETATA (il
        # handover, o un todo legale chiuso dall'operatore). Scoped alla pratica
        # corrente: un handover di un ciclo PASSATO non esclude un nuovo debito.
        # NB: NON si esclude per recovery_status=='lawyer' — è solo lo STADIO
        # (impostato appena esiste il todo legale pianificato dopo il 2°
        # sollecito): escludere per stato svuoterebbe la lista.
        overdue_invs = _overdue_invoices(session, cust.id)
        overdue_ids = [i.id for i in overdue_invs]
        delivered = _delivered_invoice_ids(session, case_obj, overdue_invs)
        undelivered = [i for i in overdue_invs if i.id not in delivered]
        if not undelivered:
            continue  # tutto il ciclo è già dal legale
        # Lista fatture PER-FATTURA: l'operatore sceglie quali consegnare.
        inv_stats = per_invoice_sollecito_stats(session, overdue_ids)
        inv_rows = [{
            "id": i.id,
            "invoice_number": i.invoice_number,
            "amount_due": float(i.amount_due or 0),
            "due_date": i.due_date.isoformat() if i.due_date else None,
            "days_overdue": int(i.days_overdue or 0),
            "sollecito_count": inv_stats.get(i.id, {}).get("count", 0),
            "delivered": i.id in delivered,
        } for i in overdue_invs]
        # Contatti FRESCHI del ciclo corrente (NON gli ereditati): servono
        # entrambi i solleciti in QUESTA pratica, e l'ultimo sollecito dev'essere
        # di questo ciclo — così un caso riaperto con soli contatti ereditati non
        # appare "pronto" senza essere stato davvero risollecitato.
        fresh = (
            session.query(RecoveryAction)
            .filter(
                RecoveryAction.case_id == case_obj.id,
                RecoveryAction.action_type.in_(CONTACT_TYPES),
                RecoveryAction.completed_at.isnot(None),
                RecoveryAction.cancelled.isnot(True),
            )
            .all()
        )
        if len(fresh) < 2:  # servono ENTRAMBI i solleciti in questo ciclo
            continue
        # "Pronto" si misura sull'ultimo sollecito delle fatture MATURE non
        # consegnate (≥2 solleciti propri): un 1° sollecito ieri sulla fattura
        # NUOVA non deve rimandare il legale per le vecchie.
        mature_last = [
            inv_stats[i.id]["last_at"] for i in undelivered
            if inv_stats.get(i.id, {}).get("count", 0) >= 2 and inv_stats[i.id].get("last_at")
        ]
        ls = max(mature_last) if mature_last else max((a.created_at for a in fresh if a.created_at), default=None)
        days_since = (today - ls.date()).days if ls else None
        has_mature = bool(mature_last)
        rows.append({
            "id": cust.id,
            "ragione_sociale": cust.ragione_sociale,
            "partita_iva": cust.partita_iva,
            "total_overdue": stats[cust.id]["total_overdue"],
            "overdue_count": stats[cust.id]["overdue_count"],
            "contact_count": len(fresh),
            "last_sollecito": ls.isoformat() if ls else None,
            "days_since_last_sollecito": days_since,
            "ready": days_since is None or days_since >= LAWYER_GRACE_DAYS,
            "recovery_status": cust.recovery_status,
            "invoices": inv_rows,
            "undelivered_total": float(sum(i.amount_due or 0 for i in undelivered)),
            "has_mature_invoice": has_mature,
        })
    rows.sort(key=lambda r: r["total_overdue"], reverse=True)
    return rows


@router.get("/candidates")
def list_candidates(session: Session = Depends(get_session)):
    """Candidati alla pratica legale: debito > soglia + entrambi i solleciti."""
    rows = _candidate_rows(session)
    return {
        "count": len(rows),
        "min_debt": LAWYER_MIN_DEBT,
        "grace_days": LAWYER_GRACE_DAYS,
        "items": rows,
    }


def _build_dossier_pdf(customer, invoices, actions, per_invoice=None, context=None):
    """Dossier legale: intestazione + tabella fatture (con date) + stato
    solleciti PER FATTURA (con note che viaggiano col debito) + timeline
    attività complessiva + totale. fpdf2, stesso stile del riepilogativo.

    `per_invoice` = {invoice_id: {"count", "last_at", "actions": [...]}}: se
    presente, il dossier riporta all'avvocato, fattura per fattura, quanti
    solleciti ha ricevuto e le note delle attività che la citano."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "Sake Company", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Sake Company srl", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Dossier per pratica legale", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, _lat1(f"Cliente: {customer.ragione_sociale}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    if customer.partita_iva:
        pdf.cell(0, 6, _lat1(f"P.IVA: {customer.partita_iva}"), new_x="LMARGIN", new_y="NEXT")
    if customer.phone:
        pdf.cell(0, 6, _lat1(f"Telefono: {customer.phone}"), new_x="LMARGIN", new_y="NEXT")
    if customer.email:
        pdf.cell(0, 6, _lat1(f"Email: {customer.email}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Documento generato il: {date.today().strftime('%d/%m/%Y')}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Fatture scadute
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Fatture affidate con il presente dossier", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(240, 240, 240)
    widths = [42, 30, 30, 30, 26]  # Fattura, Importo, Emissione, Scadenza, GG
    headers = ["N. Fattura", "Importo", "Emissione", "Scadenza", "GG Ritardo"]
    for i, h in enumerate(headers):
        pdf.cell(widths[i], 8, h, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    total = 0.0
    for inv in invoices:
        total += float(inv.amount_due)
        pdf.cell(widths[0], 7, _lat1(inv.invoice_number)[:24], border=1, align="L")
        pdf.cell(widths[1], 7, f"{float(inv.amount_due):,.2f}".replace(",", "."), border=1, align="R")
        pdf.cell(widths[2], 7, inv.issue_date.strftime("%d/%m/%Y") if inv.issue_date else "-", border=1, align="C")
        pdf.cell(widths[3], 7, inv.due_date.strftime("%d/%m/%Y") if inv.due_date else "-", border=1, align="C")
        pdf.cell(widths[4], 7, str(inv.days_overdue or 0), border=1, align="C")
        pdf.ln()
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(widths[0], 9, "TOTALE AFFIDATO", border=1, fill=True, align="R")
    pdf.cell(widths[1], 9, f"{total:,.2f}".replace(",", "."), border=1, fill=True, align="R")
    pdf.cell(widths[2] + widths[3] + widths[4], 9, "EUR", border=1, fill=True, align="L")
    pdf.ln(10)

    # Perimetro ESPLICITO (documento legale): il dossier può essere PARZIALE
    # (l'operatore sceglie le fatture). Il legale deve sapere cosa resta fuori.
    if context is not None:
        od = context.get("others_delivered") or []
        oo = context.get("others_open") or []
        ii = context.get("in_incasso") or []
        pdf.set_font("Helvetica", "I", 9)
        if ii:
            pdf.multi_cell(0, 5, _lat1(
                f"Fatture scadute coperte da assegno in attesa di incasso (NON affidate): {len(ii)} per EUR "
                + f"{sum(float(i.amount_due) for i in ii):,.2f}".replace(",", ".")
                + " - " + ", ".join(i.invoice_number for i in ii)), new_x="LMARGIN", new_y="NEXT")
        if not od and not oo:
            pdf.multi_cell(0, 5, "Il presente dossier comprende tutte le altre fatture scadute del cliente."
                           if ii else "Il presente dossier comprende tutte le fatture scadute del cliente.",
                           new_x="LMARGIN", new_y="NEXT")
        else:
            def _eur(xs):
                return f"{sum(float(i.amount_due) for i in xs):,.2f}".replace(",", ".")
            line = (f"Altre fatture scadute del cliente NON comprese nel presente dossier: "
                    f"{len(od) + len(oo)} per EUR {_eur(od + oo)}")
            if od:
                line += f" - di cui gia' affidate al legale con dossier precedenti: {len(od)} per EUR {_eur(od)}"
            if oo:
                line += f" - di cui ancora in sollecito: {len(oo)} per EUR {_eur(oo)}"
            pdf.multi_cell(0, 5, _lat1(line), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # Stato solleciti PER FATTURA — il soggetto del recupero è la fattura: per
    # ognuna, quanti solleciti ha ricevuto, l'ultimo quando, e le note delle
    # attività che la citano (la nota viaggia col debito fino al legale).
    if per_invoice:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Stato solleciti per fattura", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        for inv in invoices:
            info = per_invoice.get(inv.id) or {}
            cnt = int(info.get("count", 0) or 0)
            last_at = info.get("last_at")
            last_s = last_at.strftime("%d/%m/%Y") if last_at else "-"
            head = f"{inv.invoice_number} - {cnt} sollecit{'o' if cnt == 1 else 'i'}"
            if cnt:
                head += f" (ultimo {last_s})"
            pdf.set_font("Helvetica", "B", 9)
            pdf.multi_cell(0, 6, _lat1(head), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 8)
            for a in info.get("actions", []):
                when = a.completed_at or a.created_at or a.scheduled_date
                when_s = when.strftime("%d/%m/%Y") if when else "-"
                label = ACTION_LABELS.get(a.action_type, a.action_type or "-")
                line = f"   {when_s} - {label}"
                if a.channel:
                    line += f" ({CHANNEL_LABELS.get(a.channel, a.channel)})"
                if a.notes:
                    line += f" - {a.notes}"
                pdf.multi_cell(0, 5, _lat1(line), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
        pdf.ln(6)

    # Timeline attività di recupero
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Attività di recupero svolte", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    if not actions:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 6, "Nessuna attività registrata.", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Helvetica", "B", 9)
        # Storia COMPLETA della diligenza (anche su fatture non in questo
        # dossier): la colonna "Fatture" dice a cosa si riferisce ogni riga.
        aw = [22, 34, 40, 18, 76]  # Data, Attività, Canale, Esito, Fatture
        for i, h in enumerate(["Data", "Attività", "Canale", "Esito", "Fatture"]):
            pdf.cell(aw[i], 8, h, border=1, fill=True, align="C")
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        numbers = (context or {}).get("numbers") or {}
        for a in actions:
            when = a.completed_at or a.created_at or a.scheduled_date
            when_s = when.strftime("%d/%m/%Y") if when else "-"
            if a.invoice_ids:
                cited = ", ".join(numbers.get(i, str(i)) for i in a.invoice_ids)
            elif a.invoice_ids is None and a.action_type == "lawyer" and a.completed_at:
                cited = "tutte le scadute alla data"
            else:
                cited = "-"
            pdf.cell(aw[0], 7, when_s, border=1, align="C")
            pdf.cell(aw[1], 7, _lat1(ACTION_LABELS.get(a.action_type, a.action_type or "-"))[:22], border=1, align="L")
            pdf.cell(aw[2], 7, _lat1(CHANNEL_LABELS.get(a.channel, a.channel or "-"))[:26], border=1, align="L")
            pdf.cell(aw[3], 7, _lat1(a.outcome or "-")[:11], border=1, align="L")
            pdf.cell(aw[4], 7, _lat1(cited)[:52], border=1, align="L")
            pdf.ln()

    return pdf.output()


def _customer_dossier_files(session: Session, customer, invoice_ids=None):
    """Ritorna [(filename, bytes)] del dossier del cliente: il PDF dossier +
    i PDF delle singole fatture scadute. Se `invoice_ids` è dato, il dossier
    copre SOLO quelle fatture (scelta dell'operatore)."""
    all_overdue = _overdue_invoices(session, customer.id)
    invoices = all_overdue
    if invoice_ids is not None:
        wanted = set(invoice_ids)
        invoices = [i for i in all_overdue if i.id in wanted]
    # Contesto per il legale: cosa NON è in questo dossier (già affidato /
    # ancora in sollecito) + numeri di fattura per la colonna "Fatture".
    case_open = get_open_case(session, customer.id)
    delivered = _delivered_invoice_ids(session, case_open, all_overdue) if case_open else set()
    in_pack = {i.id for i in invoices}
    others = [i for i in all_overdue if i.id not in in_pack]
    numbers = {
        i.id: i.invoice_number
        for i in session.query(Invoice.id, Invoice.invoice_number)
        .filter(Invoice.customer_id == customer.id).all()
    }
    # Fatture coperte da ASSEGNO in attesa di incasso: non perseguibili (fuori
    # dal dossier e dalla soglia) ma FatturaPro le vede aperte → il legale
    # deve saperlo (perimetro esplicito).
    in_incasso = (
        session.query(Invoice)
        .filter(Invoice.customer_id == customer.id, overdue_clause(), in_incasso_clause(),
                Invoice.status != "disputed")
        .all()
    )
    context = {
        "others_delivered": [i for i in others if i.id in delivered],
        "others_open": [i for i in others if i.id not in delivered],
        "in_incasso": in_incasso,
        "numbers": numbers,
    }
    actions = (
        session.query(RecoveryAction)
        .filter(
            RecoveryAction.customer_id == customer.id,
            RecoveryAction.cancelled.isnot(True),
        )
        .order_by(RecoveryAction.created_at.asc())
        .all()
    )
    # Stato solleciti + note PER FATTURA (via tabella di join): riportati
    # all'avvocato fattura per fattura, così la nota viaggia col debito.
    # Degrado grazioso: se la tabella di join non c'è ancora (prod indietro di
    # una migration nella finestra iniziale del boot) il dossier si genera
    # comunque, senza la sezione per-fattura — il resto è invariato.
    inv_ids = [inv.id for inv in invoices]
    try:
        _stats = per_invoice_sollecito_stats(session, inv_ids)
        _acts = per_invoice_actions(session, inv_ids)
        per_invoice = {
            inv.id: {
                "count": _stats.get(inv.id, {}).get("count", 0),
                "last_at": _stats.get(inv.id, {}).get("last_at"),
                "actions": _acts.get(inv.id, []),
            }
            for inv in invoices
        }
    except Exception as e:
        logger.warning("Sezione solleciti per-fattura saltata: %s", e)
        session.rollback()
        per_invoice = {}
    files = [(f"dossier_{_safe(customer.ragione_sociale)}.pdf",
              bytes(_build_dossier_pdf(customer, invoices, actions, per_invoice, context)))]
    for inv in invoices:
        # Resiliente: il PDF singola fattura riusa il builder di recovery.py
        # (font core = latin-1); un carattere fuori latin-1 nel nome non deve
        # far saltare l'intero dossier — la fattura resta comunque nella
        # tabella del dossier. Si salta solo il singolo PDF.
        try:
            files.append((f"fatture/fattura_{_safe(inv.invoice_number)}.pdf",
                          bytes(_build_invoice_pdf(customer, inv))))
        except Exception:
            logger.warning("PDF fattura %s saltato (cliente %s)", inv.invoice_number, customer.id)
    return files


@router.get("/customers/{customer_id}/dossier-zip")
def dossier_zip(
    customer_id: int,
    invoice_ids: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """ZIP del dossier di UN cliente: dossier PDF + PDF delle fatture.
    `invoice_ids` (csv) = solo le fatture scelte dall'operatore."""
    customer = session.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    sel = _parse_ids(invoice_ids)
    if sel is not None:
        overdue_ids = {i.id for i in _overdue_invoices(session, customer_id)}
        unknown = [i for i in sel if i not in overdue_ids]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Fatture non scadute o non del cliente: {unknown}",
            )
    files = _customer_dossier_files(session, customer, sel)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    buf.seek(0)
    fname = f"dossier_{_safe(customer.ragione_sociale)}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/dossier-zip-all")
def dossier_zip_all(session: Session = Depends(get_session)):
    """UN unico ZIP con una CARTELLA per candidato ('prepara tutti'). La
    consegna bisettimanale in un colpo."""
    rows = _candidate_rows(session)
    if not rows:
        raise HTTPException(status_code=404, detail="Nessun candidato")
    buf = io.BytesIO()
    errors = 0
    skipped = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            customer = session.query(Customer).filter(Customer.id == r["id"]).first()
            if not customer:
                continue
            # id nel nome cartella: due ragioni sociali diverse possono
            # collassare sulla stessa forma _safe (es. "Rossi & Figli" vs
            # "Rossi, Figli") e sovrascriversi nello ZIP.
            folder = f"{customer.id}_{_safe(customer.ragione_sociale)}"
            # Resiliente: un cliente che fa fallire la generazione (dati sporchi)
            # non deve affossare l'intero pacchetto — si salta e si prosegue.
            # Selezione di default per "prepara tutti": SOLO le fatture non
            # ancora consegnate con almeno 2 solleciti propri (mature). Niente
            # fallback: al legale non si spedisce una fattura mai sollecitata.
            # Chi non ha fatture mature si salta e lo si DICHIARA (SALTATI.txt:
            # niente scarti silenziosi); l'operatore può scegliere a mano.
            sel = [i["id"] for i in r["invoices"]
                   if not i["delivered"] and i["sollecito_count"] >= 2]
            if not sel:
                skipped.append(
                    f"{customer.id} - {customer.ragione_sociale}: nessuna fattura "
                    "non consegnata con almeno 2 solleciti (scegli a mano dalla lista)"
                )
                continue
            try:
                for name, data in _customer_dossier_files(session, customer, sel):
                    zf.writestr(f"{folder}/{name}", data)
            except Exception:
                errors += 1
                logger.exception("Dossier fallito per cliente %s (%s)", customer.id, folder)
        if skipped:
            zf.writestr("SALTATI.txt", _lat1(
                "Clienti candidati SENZA fatture mature (>=2 solleciti) non consegnate: "
                "nessun dossier generato in automatico.\n\n" + "\n".join(skipped) + "\n"))
    if errors:
        logger.warning("dossier-zip-all: %d dossier saltati per errore", errors)
    if skipped:
        logger.info("dossier-zip-all: %d clienti senza fatture mature (SALTATI.txt)", len(skipped))
    buf.seek(0)
    stamp = date.today().strftime("%Y%m%d")
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="dossier_avvocato_{stamp}.zip"'},
    )


@router.post("/customers/{customer_id}/handover")
def handover_to_lawyer(
    customer_id: int,
    body: Optional[HandoverBody] = None,
    session: Session = Depends(get_session),
):
    """Segna le fatture SCELTE come CONSEGNATE all'avvocato (per-fattura).

    Registra un'azione 'lawyer' COMPLETATA che CITA le fatture consegnate
    (invoice_ids + tabella di join). Il cliente resta candidato finché ha
    scadute non ancora consegnate (caso Ferro: le vecchie al legale, le nuove
    restano in sollecito). Quando TUTTO il ciclo è consegnato → stato legale.
    Body vuoto = tutte le scadute non consegnate (legacy). Idempotente: le
    fatture già consegnate si ignorano.
    """
    customer = session.query(Customer).filter(
        Customer.id == customer_id,
        Customer.merged_into.is_(None),
    ).with_for_update().first()
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    case_open = get_open_case(session, customer_id)
    if case_open is None:
        # Nessuna pratica aperta = non è (più) un candidato: saldato o chiuso
        # tra il caricamento della lista e il clic. NON si fabbrica una pratica
        # fantasma; si chiede di aggiornare la lista.
        raise HTTPException(
            status_code=409,
            detail="Nessuna pratica aperta per questo cliente: aggiorna la lista",
        )
    overdue_objs = _overdue_invoices(session, customer_id)
    overdue_ids = [i.id for i in overdue_objs]
    delivered = _delivered_invoice_ids(session, case_open, overdue_objs)
    # Body ASSENTE = legacy "tutte le non consegnate"; una lista VUOTA
    # esplicita contraddice "l'operatore sceglie" → rifiutata.
    if body is not None and not body.invoice_ids:
        raise HTTPException(status_code=400, detail="Seleziona almeno una fattura da consegnare")
    requested = sorted(set(body.invoice_ids)) if body else []
    unknown = [i for i in requested if i not in overdue_ids]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Fatture non scadute o non del cliente: {unknown}",
        )
    target = requested or overdue_ids
    to_deliver = [i for i in target if i not in delivered]
    if not to_deliver:
        return {
            "customer_id": customer_id, "already": True,
            "delivered": sorted(delivered), "remaining": 0,
            "recovery_status": customer.recovery_status,
        }
    try:
        now = datetime.utcnow()
        action = RecoveryAction(
            customer_id=customer_id,
            case_id=case_open.id,
            action_type="lawyer",
            completed_at=now,
            outcome="handover",
            invoice_ids=to_deliver,
            notes=f"Documentazione consegnata all'avvocato ({len(to_deliver)} fatture)",
        )
        session.add(action)
        session.flush()
        set_action_invoices(session, action.id, to_deliver)
        # La pagina Avvocato È il flusso di consegna: i todo legali pendenti
        # (auto-pianificati dopo il 2° sollecito) sono ridondanti e, se
        # completati dopo una consegna PARZIALE, consegnerebbero il resto in
        # modo implicito → si annullano.
        pending_lawyer = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case_open.id,
            RecoveryAction.action_type == "lawyer",
            RecoveryAction.completed_at.is_(None),
            RecoveryAction.cancelled.isnot(True),
        ).all()
        for p in pending_lawyer:
            p.cancelled = True
            p.cancelled_reason = f"superseded_by_handover:{action.id}"
        session.flush()
        all_delivered = delivered | set(to_deliver)
        remaining = [i for i in overdue_ids if i not in all_delivered]
        if not remaining:
            # Tutto il ciclo è dal legale: stato legale, niente altre azioni.
            customer.recovery_status = "lawyer"
            customer.next_action_type = None
            customer.next_action_date = None
        else:
            # Consegna parziale: il prossimo passo è il primo todo ancora
            # pendente (se c'è); lo stato resta quello del sollecito.
            nxt = session.query(RecoveryAction).filter(
                RecoveryAction.case_id == case_open.id,
                RecoveryAction.completed_at.is_(None),
                RecoveryAction.cancelled.isnot(True),
                RecoveryAction.scheduled_date.isnot(None),
            ).order_by(RecoveryAction.scheduled_date.asc()).first()
            customer.next_action_date = nxt.scheduled_date if nxt else None
            customer.next_action_type = nxt.action_type if nxt else None
        customer.updated_at = now
        session.commit()
        return {
            "customer_id": customer_id, "already": False,
            "delivered": sorted(to_deliver), "remaining": len(remaining),
            "recovery_status": customer.recovery_status,
        }
    except Exception as e:
        logger.error(f"Errore handover avvocato: {e}", exc_info=True)
        session.rollback()
        raise
