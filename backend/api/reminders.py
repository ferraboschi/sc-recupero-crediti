"""API delle email automatiche per fattura: promemoria pre-scadenza
(/email…) e secondo avviso a N giorni dalla scadenza (/followup…): stato,
anteprima, esecuzione, ritento, registro. Le regole stanno in
backend/engine/reminder_email.py e backend/engine/followup_email.py; qui
solo trasporto."""
import logging
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.config import config
from backend.database import get_session, Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail
from backend.engine import email_status
from backend.engine import followup_email as followup
from backend.engine.agents import agents_of
from backend.connectors.resend_mail import MailSendError
from backend.engine.reminder_email import (
    AUTO_RETRY_KINDS, MODE_LABELS, MODE_REALE, SKIP_LABELS, ModeMismatch, NotFound, ReminderBusy, agent_emails,
    build_attachments, carbon_copies, normalize_email, overdue_invoices, preview, resolve_mode, retry_row,
    run_reminders, send_test, target_due_dates,
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
    # Solo «Riprova» del secondo avviso: l'operatore conferma di aver
    # controllato su Resend che l'email NON è stata consegnata.
    force: bool = False


def _serialize(row, invoice: Optional[Invoice], customer: Optional[Customer], kind: str = "promemoria",
               groups: Optional[dict] = None) -> dict:
    """Una riga del registro (promemoria o secondo avviso: stesse colonne).
    `groups` (solo secondo avviso): le fatture della stessa email, per
    `group_key` — una email con più fatture ha una riga per fattura."""
    group_key = getattr(row, "group_key", None)
    members = (groups or {}).get(group_key) or [] if group_key else []
    is_followup = kind == followup.KIND_KEY
    flags = _followup_flags(row, invoice, customer) if is_followup else {}
    return {
        "id": row.id,
        "kind": kind,
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
        # Ferma finché un operatore non preme «Riprova»? (secondo avviso: anche
        # «nessun invio tentato» si ritenta da solo)
        "needs_manual_retry": flags["needs_manual_retry"] if is_followup else (
            row.status == "sending" or (
                row.status == "failed" and (row.error_kind or "other") not in AUTO_RETRY_KINDS
            )
        ),
        # Secondo avviso: «Riprova» può creare un doppione che Resend non
        # fermerebbe (dopo un 409, o oltre un giorno): serve «Invia comunque».
        "retry_needs_confirmation": flags.get("retry_needs_confirmation", False),
        # Secondo avviso, riga ferma: il giro la riprenderà da solo?
        "will_auto_retry": flags.get("will_auto_retry", False),
        # Secondo avviso: l'indirizzo a cui andrebbe un ritento ADESSO.
        "current_to": flags.get("current_to"),
        "attempted_at": row.attempted_at.isoformat() if row.attempted_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        # Email unica per più fatture dello stesso cliente (secondo avviso):
        # le ALTRE fatture della stessa email.
        "group_key": group_key,
        "group_size": max(len(members), 1),
        "group_invoices": [m for m in members if m["row_id"] != row.id],
    }


def _followup_flags(row, invoice: Optional[Invoice], customer: Optional[Customer]) -> dict:
    """Come si presenta una riga del secondo avviso nella modalità in cui si
    invierebbe ADESSO: chi deve muoversi (operatore o giro) e con quale
    cautela."""
    send_mode = resolve_mode()[0]
    uncertain = followup._is_uncertain(row)
    stuck = followup._is_stuck(row)
    # La finestra del prossimo giro: oggi, o lunedì se oggi è weekend.
    window = followup.followup_window()
    if window is None:
        today = followup.italian_today()
        monday = datetime.combine(today + timedelta(days=7 - today.weekday()), time(10, 0), tzinfo=timezone.utc)
        window = followup.followup_window(monday)
    in_window = bool(
        window and invoice is not None and invoice.due_date is not None
        and window[0] <= invoice.due_date <= followup.merge_until(window)
    )
    # Il giro la riprende da sola se è ancora nella finestra e: l'esito è
    # «certamente non partita», oppure è una riga di prova/test e si invia in
    # reale (lì è un invio nuovo). Un incerto resta dell'operatore.
    fresh_in_real = send_mode == MODE_REALE and (row.mode or MODE_REALE) != MODE_REALE
    auto = stuck and in_window and (not uncertain or fresh_in_real)
    # A chi andrebbe un ritento ADESSO: il cliente a cui la fattura appartiene ora.
    owner = invoice.customer if invoice is not None and invoice.customer is not None else customer
    return {
        "needs_manual_retry": bool(stuck and not auto),
        "retry_needs_confirmation": bool(followup.needs_confirmation(row, mode=send_mode)),
        "will_auto_retry": bool(auto),
        "current_to": normalize_email(owner.email) if owner is not None else None,
    }


def _rows_query(session: Session, model=InvoiceReminderEmail):
    return (
        session.query(model, Invoice, Customer)
        .outerjoin(Invoice, Invoice.id == model.invoice_id)
        .outerjoin(Customer, Customer.id == model.customer_id)
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
    # I promemoria FERMI che «Riprova» accetta ancora (scadenza non passata)
    # si vedono sempre, anche oltre le ultime `limit` righe: «Da gestire in
    # Sistema» non deve mai puntare a una riga che qui non c'è.
    listed = {r.id for r, _i, _c in rows}
    stuck = (
        _rows_query(session)
        .filter(
            InvoiceReminderEmail.status.in_(("failed", "sending")),
            InvoiceReminderEmail.due_date >= followup.italian_today(),
        )
        .order_by(InvoiceReminderEmail.created_at.desc(), InvoiceReminderEmail.id.desc())
        .all()
    )
    rows = [found for found in stuck if found[0].id not in listed] + rows
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
    items = []
    for model, kind in ((InvoiceReminderEmail, "promemoria"), (InvoiceFollowupEmail, followup.KIND_KEY)):
        try:
            query = _rows_query(session, model)
            if customer_id is not None:
                query = query.filter(model.customer_id == customer_id)
            rows = query.order_by(model.created_at.desc(), model.id.desc()).limit(limit).all()
            groups = followup.group_members(session, [r for r, _i, _c in rows]) if kind == followup.KIND_KEY else None
            items.extend(_serialize(r, i, c, kind, groups) for r, i, c in rows)
        except Exception as e:
            if kind != followup.KIND_KEY:
                raise
            # Il registro dei secondi avvisi non deve mai nascondere quello dei
            # promemoria (già letto qui sopra): si logga e si prosegue.
            logger.error(f"Registro dei secondi avvisi non leggibile: {e}", exc_info=True)
            session.rollback()
    # Promemoria e secondi avvisi insieme, dal più recente (le date ISO si
    # ordinano come testo; una riga senza data va in fondo).
    items.sort(key=lambda it: (it["created_at"] or "", it["kind"], it["id"]), reverse=True)
    items = items[:limit]
    return {"items": items, "count": len(items)}


@router.get("/email/customer-status")
def email_customer_status(session: Session = Depends(get_session)):
    """Le email automatiche da mostrare sotto il nome di ogni cliente
    (promemoria e secondo avviso delle fatture ancora da saldare). Sola
    lettura: regole in backend/engine/email_status.py."""
    return email_status.customer_status(session)


@router.get("/email/sent")
def email_sent_log(
    days: int = Query(14, ge=email_status.MIN_LOG_DAYS, le=email_status.MAX_LOG_DAYS),
    session: Session = Depends(get_session),
):
    """Registro unico «Email inviate»: promemoria e secondi avvisi reali,
    giorno per giorno, più le email ferme da gestire. Sola lettura."""
    return email_status.sent_log(session, days=days)


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


# ── Secondo avviso (fattura scaduta da N giorni) ──────────────────────

def _followup_mode_mismatch(e: ModeMismatch) -> HTTPException:
    labels = followup.FOLLOWUP_MODE_LABELS
    return HTTPException(
        status_code=409,
        detail=(
            f"La modalità sul server è ora «{labels.get(e.actual, e.actual)}», non "
            f"«{labels.get(e.expected, e.expected)}»: ricarica la pagina e conferma di nuovo."
        ),
    )


def _followup_row(session: Session, row_id: int):
    found = _rows_query(session, InvoiceFollowupEmail).filter(InvoiceFollowupEmail.id == row_id).first()
    if not found:
        raise HTTPException(status_code=404, detail="Secondo avviso non trovato")
    return found


@router.get("/followup")
def followup_email_status(
    limit: int = Query(30, ge=1, le=200),
    session: Session = Depends(get_session),
):
    """Stato del secondo avviso: acceso/spento, modalità, finestra delle
    scadenze, affidabilità del dato «non pagata» (sync), cosa partirà al
    prossimo giro (anteprima con testo) e gli ultimi avvisi registrati."""
    mode, missing = followup.resolve_followup_mode()
    send_mode = resolve_mode()[0]
    window = followup.followup_window()
    rows = (
        _rows_query(session, InvoiceFollowupEmail)
        .order_by(InvoiceFollowupEmail.created_at.desc(), InvoiceFollowupEmail.id.desc())
        .limit(limit)
        .all()
    )
    stuck = followup.stuck_rows(session)
    groups = followup.group_members(session, [r for r, _i, _c in rows] + [r for r, _i, _c in stuck])
    return {
        "enabled": mode != followup.MODE_OFF,
        "mode": mode,
        "mode_label": followup.FOLLOWUP_MODE_LABELS.get(mode, mode),
        # Modalità con cui partirebbe da acceso (quella del promemoria).
        "send_mode": send_mode,
        "send_mode_label": MODE_LABELS.get(send_mode, send_mode),
        "missing_config": missing,
        "days_after_due": followup.followup_days(),
        "lookback_days": followup.followup_lookback(),
        "window": [d.isoformat() for d in window] if window else None,
        # Una email per cliente: vi entrano anche le sue fatture con la
        # scadenza fino a questo giorno (± un giorno dall'ultima della finestra).
        "near_days": followup.NEAR_DUE_DAYS,
        "merge_until": followup.merge_until(window).isoformat() if window else None,
        "weekend": window is None,
        "sync": followup.sync_gate(session),
        "schedule": f"{config.REMINDER_EMAIL_HOUR:02d}:{config.REMINDER_EMAIL_MINUTE:02d} {config.TIMEZONE}",
        "sender": config.REMINDER_EMAIL_FROM or None,
        "test_to": config.REMINDER_EMAIL_TEST_TO or None,
        "cc_effective": carbon_copies(None, config),
        "agent_emails": agent_emails(),
        "preview": followup.preview_followups(session, mode=mode),
        "recent": [_serialize(r, i, c, followup.KIND_KEY, groups) for r, i, c in rows],
        # TUTTE le righe ferme (fallite o interrotte) di fatture ancora da
        # sollecitare, qualunque età abbiano: non devono uscire di vista
        # quando il registro qui sopra si riempie.
        "stuck": [_serialize(r, i, c, followup.KIND_KEY, groups) for r, i, c in stuck],
    }


@router.post("/followup/run")
def followup_email_run(body: Optional[RunBody] = None, session: Session = Depends(get_session)):
    """Esegue ADESSO il giro del secondo avviso (stessa logica del job del
    mattino). Da spento, nel weekend o con il sync non aggiornato non fa
    nulla e lo dice in `blocked`."""
    expected = body.expected_mode if body else None
    try:
        return followup.run_followups(session, expected_mode=expected)
    except ReminderBusy:
        raise HTTPException(status_code=409, detail="Un giro di email è già in corso")
    except ModeMismatch as e:
        raise _followup_mode_mismatch(e)


@router.post("/followup/test")
def followup_email_test(body: TestSendBody, session: Session = Depends(get_session)):
    """Invia a un indirizzo interno il secondo avviso che il cliente
    riceverebbe per la fattura indicata — l'email unica con le altre sue
    fatture del giro di oggi, se ce ne sono ([PROVA], nessuna copia,
    registro intoccato). Funziona anche da spento."""
    try:
        return followup.send_followup_test(session, body.invoice_id, body.to)
    except NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailSendError as e:  # messaggio già senza segreti
        raise HTTPException(status_code=502, detail=f"Invio di prova fallito: {e}")
    except Exception as e:
        logger.error(f"Invio di prova del secondo avviso fallito (errore interno): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Invio di prova fallito: errore interno, vedi i log")


@router.post("/followup/{row_id}/retry")
def followup_email_retry(row_id: int, body: Optional[RunBody] = None, session: Session = Depends(get_session)):
    """Ritenta un secondo avviso fermo (fallito o interrotto), con le altre
    fatture della stessa email ancora ferme: resta UNA email."""
    row, _invoice, _customer = _followup_row(session, row_id)
    expected = body.expected_mode if body else None
    try:
        return followup.retry_followup_row(session, row, expected_mode=expected, force=bool(body and body.force))
    except ReminderBusy:
        raise HTTPException(status_code=409, detail="Un giro di email è già in corso")
    except ModeMismatch as e:
        raise _followup_mode_mismatch(e)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/followup/{row_id}")
def followup_email_detail(row_id: int, session: Session = Depends(get_session)):
    row, invoice, customer = _followup_row(session, row_id)
    data = _serialize(row, invoice, customer, followup.KIND_KEY, followup.group_members(session, [row]))
    data["body_text"] = row.body_text
    return data


@router.get("/followup/{row_id}/attachments/{index}")
def followup_email_attachment(row_id: int, index: int, session: Session = Depends(get_session)):
    """Rigenera l'allegato n-esimo (una copia di cortesia per ogni fattura
    dell'email, poi il riepilogo delle scadute) dai dati attuali, per
    controllo: può differire da quello inviato se nel frattempo le scadute
    sono cambiate."""
    row, invoice, customer = _followup_row(session, row_id)
    if invoice is None or customer is None:
        raise HTTPException(status_code=404, detail="Fattura o cliente non più disponibili")
    gone = HTTPException(
        status_code=404,
        detail="Allegato non più disponibile: viene rigenerato dai dati attuali e le scadute sono cambiate",
    )
    invoices = followup.row_invoices(session, row)
    overdue = followup.other_overdue_invoices(session, customer.id, exclude_ids=[i.id for i in invoices])
    attachments = followup.build_followup_attachments(customer, invoices, overdue)
    stored = row.attachments_json or []
    if index < 0:
        raise gone
    if stored:
        # L'allegato si cerca per NOME (quello registrato all'invio), non per
        # posizione: se nel frattempo l'ordine o l'insieme è cambiato non si
        # consegna un altro documento sotto il nome di questo.
        if index >= len(stored):
            raise gone
        found = next(((n, c) for n, c in attachments if n == stored[index]), None)
        if found is None:
            raise gone
        name, content = found
    else:
        if index >= len(attachments):
            raise gone
        name, content = attachments[index]
    return StreamingResponse(
        BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
