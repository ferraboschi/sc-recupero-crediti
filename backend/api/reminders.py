"""API dei promemoria email pre-scadenza (stato, anteprima, esecuzione,
ritento, registro). Vedi backend/engine/reminder_email.py per le regole;
qui solo trasporto."""
import logging
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.config import config
from backend.database import get_session, Customer, Invoice, InvoiceReminderEmail
from backend.engine.agents import agents_of
from backend.connectors.resend_mail import MailSendError
from backend.engine.reminder_email import (
    AUTO_RETRY_KINDS, MODE_LABELS, SKIP_LABELS, ModeMismatch, NotFound, ReminderBusy, agent_emails,
    build_attachments, carbon_copies, overdue_invoices, preview, resolve_mode, retry_row, run_reminders,
    send_test, target_due_dates,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class TestSendBody(BaseModel):
    invoice_id: int
    to: str


class RunBody(BaseModel):
    # La modalità che l'operatore ha letto e confermato a video: se il server
    # ne risolve un'altra (env cambiate), 409 — mai un invio "a sorpresa".
    expected_mode: Optional[str] = None


def _serialize(row: InvoiceReminderEmail, invoice: Optional[Invoice], customer: Optional[Customer]) -> dict:
    return {
        "id": row.id,
        "invoice_id": row.invoice_id,
        "invoice_number": invoice.invoice_number if invoice else None,
        "customer_id": row.customer_id,
        "ragione_sociale": customer.ragione_sociale if customer else (invoice.customer_name_raw if invoice else None),
        "agents": agents_of(customer),
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "status": row.status,
        "mode": row.mode,
        "skip_reason": row.skip_reason,
        "skip_label": SKIP_LABELS.get(row.skip_reason) if row.skip_reason else None,
        "to_email": row.to_email,
        "cc": row.cc_json or [],
        "original_to": row.original_to,
        "subject": row.subject,
        "amount_due": float(row.amount_due or 0),
        "overdue_count": row.overdue_count or 0,
        "overdue_total": float(row.overdue_total or 0),
        "attachments": row.attachments_json or [],
        "provider_message_id": row.provider_message_id,
        "error": row.error,
        "error_kind": row.error_kind,
        # Ferma finché un operatore non preme «Riprova»?
        "needs_manual_retry": row.status == "sending" or (
            row.status == "failed" and (row.error_kind or "other") not in AUTO_RETRY_KINDS
        ),
        "attempted_at": row.attempted_at.isoformat() if row.attempted_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
    }


def _rows_query(session: Session):
    return (
        session.query(InvoiceReminderEmail, Invoice, Customer)
        .outerjoin(Invoice, Invoice.id == InvoiceReminderEmail.invoice_id)
        .outerjoin(Customer, Customer.id == InvoiceReminderEmail.customer_id)
    )


def _mode_mismatch(e: ModeMismatch) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"La modalità sul server è ora «{MODE_LABELS.get(e.actual, e.actual)}», non "
            f"«{MODE_LABELS.get(e.expected, e.expected)}»: ricarica la pagina e conferma di nuovo."
        ),
    )


@router.get("/email")
def reminder_email_status(
    limit: int = Query(30, ge=1, le=200),
    session: Session = Depends(get_session),
):
    """Stato della funzione: modalità, configurazione mancante, cosa partirà
    al prossimo giro (anteprima con testo) e le ultime email registrate."""
    mode, missing = resolve_mode()
    dates = target_due_dates()
    rows = (
        _rows_query(session)
        .order_by(InvoiceReminderEmail.created_at.desc(), InvoiceReminderEmail.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "missing_config": missing,
        "sender": config.REMINDER_EMAIL_FROM or None,
        "reply_to": config.REMINDER_EMAIL_REPLY_TO or None,
        "bcc": config.REMINDER_EMAIL_BCC or None,
        "cc": config.REMINDER_EMAIL_CC or None,
        "cc_effective": carbon_copies(None, config),  # gli indirizzi fissi davvero usati (validi)
        "agent_emails": agent_emails(),
        "test_to": config.REMINDER_EMAIL_TEST_TO or None,
        "company": {
            "name": config.COMPANY_NAME, "vat": config.COMPANY_VAT or None,
            "address": config.COMPANY_ADDRESS or None,
            "iban_tail": config.COMPANY_IBAN[-4:] if config.COMPANY_IBAN else None,
        },
        "schedule": f"{config.REMINDER_EMAIL_HOUR:02d}:{config.REMINDER_EMAIL_MINUTE:02d} {config.TIMEZONE}",
        "target_due_date": dates[0].isoformat() if dates else None,
        "target_due_dates": [d.isoformat() for d in dates],
        "weekend": not dates,
        "preview": preview(session, due_dates=dates, mode=mode) if dates else [],
        "recent": [_serialize(r, i, c) for r, i, c in rows],
    }


@router.post("/email/run")
def reminder_email_run(body: Optional[RunBody] = None, session: Session = Depends(get_session)):
    """Esegue ADESSO il giro per la scadenza di domani (stessa logica del job
    delle 09:15). In modalità prova prepara soltanto."""
    expected = body.expected_mode if body else None
    try:
        return run_reminders(session, expected_mode=expected)
    except ReminderBusy:
        raise HTTPException(status_code=409, detail="Un giro di promemoria è già in corso")
    except ModeMismatch as e:
        raise _mode_mismatch(e)


@router.post("/email/test")
def reminder_email_test(body: TestSendBody, session: Session = Depends(get_session)):
    """Invia a un indirizzo scelto l'email che il cliente riceverebbe per la
    fattura indicata ([PROVA], nessuna copia, registro intoccato)."""
    try:
        return send_test(session, body.invoice_id, body.to)
    except NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailSendError as e:  # messaggio già senza segreti
        raise HTTPException(status_code=502, detail=f"Invio di prova fallito: {e}")
    except Exception as e:
        logger.error(f"Invio di prova fallito (errore interno): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Invio di prova fallito: errore interno, vedi i log")


@router.post("/email/{row_id}/retry")
def reminder_email_retry(row_id: int, body: Optional[RunBody] = None, session: Session = Depends(get_session)):
    """Ritenta un promemoria fermo (fallito o interrotto), anche di una
    scadenza passata."""
    row = session.query(InvoiceReminderEmail).filter(InvoiceReminderEmail.id == row_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Promemoria non trovato")
    expected = body.expected_mode if body else None
    try:
        return retry_row(session, row, expected_mode=expected)
    except ReminderBusy:
        raise HTTPException(status_code=409, detail="Un giro di promemoria è già in corso")
    except ModeMismatch as e:
        raise _mode_mismatch(e)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/email/log")
def reminder_email_log(
    customer_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
):
    query = _rows_query(session)
    if customer_id is not None:
        query = query.filter(InvoiceReminderEmail.customer_id == customer_id)
    rows = (
        query.order_by(InvoiceReminderEmail.created_at.desc(), InvoiceReminderEmail.id.desc())
        .limit(limit)
        .all()
    )
    return {"items": [_serialize(r, i, c) for r, i, c in rows], "count": len(rows)}


@router.get("/email/{row_id}")
def reminder_email_detail(row_id: int, session: Session = Depends(get_session)):
    found = _rows_query(session).filter(InvoiceReminderEmail.id == row_id).first()
    if not found:
        raise HTTPException(status_code=404, detail="Promemoria non trovato")
    row, invoice, customer = found
    data = _serialize(row, invoice, customer)
    data["body_text"] = row.body_text
    return data


@router.get("/email/{row_id}/attachments/{index}")
def reminder_email_attachment(row_id: int, index: int, session: Session = Depends(get_session)):
    """Rigenera l'allegato n-esimo (0 = copia di cortesia, 1 = riepilogo
    scadute) dai dati attuali, per controllo: può differire da quello
    inviato se nel frattempo le scadute sono cambiate."""
    found = _rows_query(session).filter(InvoiceReminderEmail.id == row_id).first()
    if not found:
        raise HTTPException(status_code=404, detail="Promemoria non trovato")
    row, invoice, customer = found
    if invoice is None or customer is None:
        raise HTTPException(status_code=404, detail="Fattura o cliente non più disponibili")
    overdue = overdue_invoices(session, customer.id, exclude_id=invoice.id)
    attachments = build_attachments(customer, invoice, overdue)
    if index < 0 or index >= len(attachments):
        raise HTTPException(
            status_code=404,
            detail="Allegato non più disponibile: viene rigenerato dai dati attuali e le scadute sono cambiate",
        )
    name, content = attachments[index]
    return StreamingResponse(
        BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
