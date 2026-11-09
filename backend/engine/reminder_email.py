"""Promemoria email il giorno PRIMA della scadenza di una fattura.

Decisione owner (9/11/2026): per ogni fattura non pagata in scadenza domani
il cliente riceve una email con:
- il PDF della fattura in scadenza (copia di cortesia generata dall'app);
- se ha anche fatture GIÀ scadute e non saldate, un secondo PDF con il
  riepilogo e il totale, citato nel testo ("questa fattura vale X; il
  riepilogo delle scadute a oggi vale Y").
Una email per fattura: cinque fatture in scadenza = cinque email.

Modalità (risolta dalla configurazione, mai implicita):
- prova  → le email vengono COMPOSTE e REGISTRATE (status prepared) ma non
           inviate: è lo stato di default finché l'owner non configura
           REMINDER_EMAIL_ENABLED, RESEND_API_KEY e REMINDER_EMAIL_FROM;
- test   → REMINDER_EMAIL_TEST_TO valorizzato: tutto va a quell'indirizzo
           (il destinatario vero resta registrato in original_to);
- reale  → invio ai clienti.

Idempotenza: una riga per (fattura, data di scadenza) — `invoice_reminder_emails`
— quindi il job può girare più volte al giorno senza doppioni; solo le righe
`failed` vengono ritentate.

Esclusioni (registrate come `skipped` con motivo, mai scartate in silenzio):
cliente escluso / unito / dall'avvocato, fattura con assegno in incasso,
cliente senza email. Non sono nemmeno candidate: pagate, annullate (void),
contestate.
"""
import html
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from backend.config import config
from backend.database import Customer, Invoice, InvoiceReminderEmail

logger = logging.getLogger(__name__)

MODE_PROVA = "prova"
MODE_TEST = "test"
MODE_REALE = "reale"

STATUS_PREPARED = "prepared"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Stati fattura che NON sono crediti da ricordare.
NOT_REMINDABLE_STATUSES = ("paid", "void", "disputed")

SKIP_LABELS = {
    "senza_cliente": "Fattura senza cliente",
    "cliente_unito": "Scheda unita a un'altra",
    "cliente_escluso": "Cliente escluso dal recupero",
    "avvocato": "Pratica dall'avvocato",
    "in_incasso": "Assegno in incasso",
    "senza_email": "Cliente senza email",
}

MODE_LABELS = {
    MODE_PROVA: "Prova (nessun invio)",
    MODE_TEST: "Test (tutto a un indirizzo di prova)",
    MODE_REALE: "Reale (ai clienti)",
}


# ── Configurazione e calendario ───────────────────────────────────────

def resolve_mode(cfg=None) -> Tuple[str, List[str]]:
    """Modalità effettiva + elenco delle variabili mancanti per l'invio reale."""
    cfg = cfg or config
    missing = []
    if not getattr(cfg, "REMINDER_EMAIL_ENABLED", False):
        missing.append("REMINDER_EMAIL_ENABLED")
    if not getattr(cfg, "RESEND_API_KEY", ""):
        missing.append("RESEND_API_KEY")
    if not getattr(cfg, "REMINDER_EMAIL_FROM", ""):
        missing.append("REMINDER_EMAIL_FROM")
    if missing:
        return MODE_PROVA, missing
    if (getattr(cfg, "REMINDER_EMAIL_TEST_TO", "") or "").strip():
        return MODE_TEST, []
    return MODE_REALE, []


def italian_today(now_utc: Optional[datetime] = None) -> date:
    """Giorno di calendario italiano (il server gira in UTC)."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(config.TIMEZONE)).date()


def target_due_date(now_utc: Optional[datetime] = None) -> date:
    """La scadenza a cui si riferisce il giro di oggi: DOMANI (ora italiana)."""
    return italian_today(now_utc) + timedelta(days=1)


# ── Formattazione ─────────────────────────────────────────────────────

def fmt_eur(value) -> str:
    amount = float(value or 0)
    text = f"{amount:,.2f}"  # 1,234.56
    text = text.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    return f"{text} €"


def fmt_date(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def safe_filename(value: str, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("_")
    return (cleaned or "documento")[:max_len]


# ── Selezione ─────────────────────────────────────────────────────────

def candidate_invoices(session: Session, due_date: date) -> List[Invoice]:
    """Fatture in scadenza nel giorno dato, non pagate/annullate/contestate."""
    return (
        session.query(Invoice)
        .filter(
            Invoice.due_date == due_date,
            Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
            Invoice.amount_due > 0,
            Invoice.customer_id.isnot(None),
        )
        .order_by(Invoice.customer_id.asc(), Invoice.invoice_number.asc())
        .all()
    )


def skip_reason(invoice: Invoice, customer: Optional[Customer]) -> Optional[str]:
    if customer is None:
        return "senza_cliente"
    if customer.merged_into is not None:
        return "cliente_unito"
    if customer.excluded:
        return "cliente_escluso"
    if (customer.recovery_status or "") == "lawyer":
        return "avvocato"
    if getattr(invoice, "payment_pending", None) and getattr(invoice, "bounced_at", None) is None:
        return "in_incasso"
    if not (customer.email or "").strip():
        return "senza_email"
    return None


def overdue_invoices(session: Session, customer_id: int, exclude_id: Optional[int] = None) -> List[Invoice]:
    """Le scadute e non saldate del cliente (stessa definizione della cascata:
    non pagate/annullate/contestate, giorni di ritardo > 0, niente assegni in
    incasso), escludendo la fattura oggetto del promemoria."""
    query = session.query(Invoice).filter(
        Invoice.customer_id == customer_id,
        Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
        Invoice.days_overdue > 0,
        Invoice.amount_due > 0,
    )
    if exclude_id is not None:
        query = query.filter(Invoice.id != exclude_id)
    rows = query.order_by(Invoice.issue_date.asc(), Invoice.invoice_number.asc()).all()
    return [
        inv for inv in rows
        if not (getattr(inv, "payment_pending", None) and getattr(inv, "bounced_at", None) is None)
    ]


# ── Composizione ──────────────────────────────────────────────────────

def compose(customer: Customer, invoice: Invoice, overdue: List[Invoice]) -> dict:
    """Oggetto e testo (testo semplice + HTML equivalente). Testo in italiano,
    approvato dall'owner prima dell'accensione dell'invio reale."""
    name = (customer.ragione_sociale or "").strip() or "Cliente"
    importo = fmt_eur(invoice.amount_due)
    scadenza = fmt_date(invoice.due_date)
    emissione = f" del {fmt_date(invoice.issue_date)}" if invoice.issue_date else ""
    subject = f"Promemoria: fattura {invoice.invoice_number} in scadenza il {scadenza} ({importo})"

    total = round(sum(float(i.amount_due or 0) for i in overdue), 2)
    paragraphs = [
        f"Gentile {name},",
        (
            f"le ricordiamo che la fattura n. {invoice.invoice_number}{emissione}, "
            f"di {importo}, scade domani, {scadenza}.\n"
            "In allegato trova la copia della fattura."
        ),
    ]
    if overdue:
        n = len(overdue)
        paragraphs.append(
            f"Le segnaliamo inoltre che a oggi risultano scadute e non ancora saldate "
            f"{n} {'fattura' if n == 1 else 'fatture'} per un totale di {fmt_eur(total)}: "
            "nel secondo allegato trova il riepilogo completo. "
            "La preghiamo di provvedere al saldo o di contattarci per concordare le modalità."
        )
    paragraphs.append("Se ha già provveduto al pagamento, consideri nullo questo promemoria.")
    paragraphs.append("Cordiali saluti,\nSake Company srl - Amministrazione")

    text = "\n\n".join(paragraphs)
    html_body = "".join(
        "<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs
    )
    return {
        "subject": subject,
        "text": text,
        "html": html_body,
        "overdue_count": len(overdue),
        "overdue_total": total,
    }


def build_attachments(customer: Customer, invoice: Invoice, overdue: List[Invoice]) -> List[Tuple[str, bytes]]:
    """PDF della fattura + (se servono) riepilogo delle scadute. Import locale:
    i generatori vivono in backend.api.recovery (evita cicli di import)."""
    from backend.api.recovery import _build_invoice_pdf, _build_riepilogativo_pdf

    out = [(
        f"fattura_{safe_filename(invoice.invoice_number)}.pdf",
        bytes(_build_invoice_pdf(customer, invoice)),
    )]
    if overdue:
        out.append((
            f"riepilogo_scadute_{safe_filename(customer.ragione_sociale or 'cliente')}.pdf",
            bytes(_build_riepilogativo_pdf(customer, overdue)),
        ))
    return out


# ── Esecuzione ────────────────────────────────────────────────────────

def _item(row: InvoiceReminderEmail, invoice: Invoice, customer: Optional[Customer]) -> dict:
    return {
        "id": row.id,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "customer_id": customer.id if customer else None,
        "ragione_sociale": customer.ragione_sociale if customer else invoice.customer_name_raw,
        "status": row.status,
        "skip_reason": row.skip_reason,
        "to_email": row.to_email,
        "amount_due": float(invoice.amount_due or 0),
        "overdue_count": row.overdue_count or 0,
        "overdue_total": float(row.overdue_total or 0),
        "error": row.error,
    }


def preview(session: Session, due_date: Optional[date] = None, now_utc: Optional[datetime] = None) -> List[dict]:
    """Cosa farebbe il giro per la scadenza data: NESSUNA scrittura."""
    due = due_date or target_due_date(now_utc)
    out = []
    for inv in candidate_invoices(session, due):
        customer = inv.customer
        existing = session.query(InvoiceReminderEmail).filter_by(invoice_id=inv.id, due_date=due).first()
        reason = skip_reason(inv, customer)
        entry = {
            "invoice_id": inv.id,
            "invoice_number": inv.invoice_number,
            "customer_id": customer.id if customer else None,
            "ragione_sociale": customer.ragione_sociale if customer else inv.customer_name_raw,
            "to_email": (customer.email or "").strip() if customer else None,
            "amount_due": float(inv.amount_due or 0),
            "due_date": due.isoformat(),
            "skip_reason": reason,
            "skip_label": SKIP_LABELS.get(reason) if reason else None,
            "already": existing.status if existing else None,
            "overdue_count": 0,
            "overdue_total": 0.0,
            "subject": None,
            "text": None,
        }
        if not reason:
            overdue = overdue_invoices(session, customer.id, exclude_id=inv.id)
            msg = compose(customer, inv, overdue)
            entry.update({
                "overdue_count": msg["overdue_count"],
                "overdue_total": msg["overdue_total"],
                "subject": msg["subject"],
                "text": msg["text"],
            })
        out.append(entry)
    return out


def run_reminders(
    session: Session,
    *,
    now_utc: Optional[datetime] = None,
    due_date: Optional[date] = None,
    mailer=None,
    cfg=None,
    mode: Optional[str] = None,
) -> dict:
    """Il giro quotidiano: compone, registra e (fuori dalla prova) invia."""
    cfg = cfg or config
    due = due_date or target_due_date(now_utc)
    resolved, missing = resolve_mode(cfg)
    mode = mode or resolved
    if mode not in (MODE_PROVA, MODE_TEST, MODE_REALE):
        raise ValueError(f"modalità sconosciuta: {mode}")
    if mode != MODE_PROVA and mailer is None:
        from backend.connectors.resend_mail import ResendMailer
        mailer = ResendMailer(cfg.RESEND_API_KEY, getattr(cfg, "RESEND_API_URL", None))

    summary = {
        "due_date": due.isoformat(), "mode": mode, "missing_config": missing,
        "candidates": 0, "sent": 0, "prepared": 0, "skipped": 0, "failed": 0,
        "already": 0, "items": [],
    }
    test_to = (getattr(cfg, "REMINDER_EMAIL_TEST_TO", "") or "").strip()

    for inv in candidate_invoices(session, due):
        summary["candidates"] += 1
        customer = inv.customer
        existing = session.query(InvoiceReminderEmail).filter_by(invoice_id=inv.id, due_date=due).first()
        if existing is not None and existing.status != STATUS_FAILED:
            summary["already"] += 1
            continue
        row = existing or InvoiceReminderEmail(invoice_id=inv.id, due_date=due)
        row.customer_id = customer.id if customer else None
        row.mode = mode
        row.amount_due = float(inv.amount_due or 0)
        row.error = None

        reason = skip_reason(inv, customer)
        if reason:
            row.status = STATUS_SKIPPED
            row.skip_reason = reason
            row.to_email = (customer.email or "").strip() or None if customer else None
            session.add(row)
            session.commit()
            summary["skipped"] += 1
            summary["items"].append(_item(row, inv, customer))
            continue

        overdue = overdue_invoices(session, customer.id, exclude_id=inv.id)
        msg = compose(customer, inv, overdue)
        real_to = customer.email.strip()
        row.skip_reason = None
        row.subject = msg["subject"]
        row.body_text = msg["text"]
        row.overdue_count = msg["overdue_count"]
        row.overdue_total = msg["overdue_total"]
        row.original_to = real_to
        row.to_email = test_to if mode == MODE_TEST else real_to

        try:
            attachments = build_attachments(customer, inv, overdue)
        except Exception as e:  # il PDF non deve far cadere il giro
            logger.error(f"Promemoria {inv.invoice_number}: PDF non generato: {e}", exc_info=True)
            row.status = STATUS_FAILED
            row.error = f"PDF non generato: {e}"[:500]
            session.add(row)
            session.commit()
            summary["failed"] += 1
            summary["items"].append(_item(row, inv, customer))
            continue
        row.attachments_json = [name for name, _ in attachments]

        if mode == MODE_PROVA:
            row.status = STATUS_PREPARED
            summary["prepared"] += 1
        else:
            try:
                message_id = mailer.send(
                    sender=cfg.REMINDER_EMAIL_FROM,
                    to=row.to_email,
                    subject=msg["subject"],
                    text=msg["text"],
                    html=msg["html"],
                    reply_to=(getattr(cfg, "REMINDER_EMAIL_REPLY_TO", "") or None),
                    bcc=(getattr(cfg, "REMINDER_EMAIL_BCC", "") or None) if mode == MODE_REALE else None,
                    attachments=attachments,
                )
                row.status = STATUS_SENT
                row.sent_at = datetime.utcnow()
                row.provider_message_id = message_id or None
                summary["sent"] += 1
            except Exception as e:  # messaggio già privo di segreti (MailSendError)
                logger.error(f"Promemoria {inv.invoice_number}: invio fallito: {e}")
                row.status = STATUS_FAILED
                row.error = str(e)[:500]
                summary["failed"] += 1
        session.add(row)
        session.commit()
        summary["items"].append(_item(row, inv, customer))

    logger.info(
        "Promemoria email (scadenza %s, modalità %s): candidate=%d inviate=%d preparate=%d "
        "saltate=%d fallite=%d già_fatte=%d",
        summary["due_date"], mode, summary["candidates"], summary["sent"], summary["prepared"],
        summary["skipped"], summary["failed"], summary["already"],
    )
    return summary
