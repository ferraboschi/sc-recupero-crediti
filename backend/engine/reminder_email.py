"""Promemoria email il giorno PRIMA della scadenza di una fattura.

Decisione owner (9/11/2026): per ogni fattura non pagata in scadenza domani
il cliente riceve una email con:
- la copia di cortesia della fattura in scadenza (PDF generato dall'app);
- se ha anche fatture GIÀ scadute e non saldate, un secondo PDF con il
  riepilogo e il totale, citato nel testo ("questa fattura vale X; il
  riepilogo delle scadute a oggi vale Y").
Una email per fattura: cinque fatture in scadenza = cinque email.

Modalità (risolta dalla configurazione, mai implicita):
- prova  → le email vengono COMPOSTE e REGISTRATE (status prepared) ma non
           inviate: è lo stato di default finché l'owner non imposta
           REMINDER_EMAIL_ENABLED=true e RESEND_API_KEY (il mittente ha un
           default: fatture@sakecompany.com);
- test   → REMINDER_EMAIL_TEST_TO valorizzato: tutto va a quell'indirizzo
           (il destinatario vero resta registrato in original_to);
- reale  → invio ai clienti.

Nessun invio doppio al cliente, per costruzione:
- una riga per (fattura, scadenza) in `invoice_reminder_emails` (vincolo
  unico) che viene RISERVATA (status sending) e salvata PRIMA dell'invio:
  due giri concorrenti (job + pulsante) non possono inviare entrambi, il
  secondo trova il vincolo e passa oltre;
- Idempotency-Key per (fattura, scadenza, modalità) sulla chiamata a
  Resend: anche un ritento dopo un timeout non duplica l'email;
- si ritentano in automatico solo i fallimenti certamente senza invio
  (PDF non generato, rifiuto di validazione del provider); timeout di rete
  e crash a metà restano fermi finché un operatore non preme «Riprova»;
- un solo giro alla volta (lock condiviso da job e API).

Esclusioni (registrate come `skipped` con motivo, mai scartate in silenzio):
cliente escluso / unito / dall'avvocato, assegno in incasso, scadenza
stimata (non letta dallo scadenzario), fattura forse già pagata (sparita
dalla lista FatturaPro ma non ancora confermata), abbinamento fattura→cliente
da verificare (audit warn/bad non revisionato), email assente o malformata.
Non sono nemmeno candidate: pagate, annullate (void), contestate.
"""
import html
import logging
import re
import threading
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import config
from backend.database import Customer, Invoice, InvoiceReminderEmail
from backend.engine.format import fmt_date, fmt_eur, italian_today, safe_filename  # noqa: F401 (riesportati)
from backend.engine.verify import verify_invoice_customer

logger = logging.getLogger(__name__)

MODE_PROVA = "prova"
MODE_TEST = "test"
MODE_REALE = "reale"

STATUS_PREPARED = "prepared"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Tipo di errore: decide se il ritento automatico è sicuro.
ERROR_PDF = "pdf"                # PDF non generato → nessun invio tentato
ERROR_VALIDATION = "validation"  # Resend 4xx → invio certamente rifiutato
ERROR_PROVIDER = "provider"      # Resend 5xx → stato incerto
ERROR_NETWORK = "network"        # timeout/connessione → stato incerto
ERROR_OTHER = "other"
AUTO_RETRY_KINDS = (ERROR_PDF, ERROR_VALIDATION)

# Stati fattura che NON sono crediti da ricordare.
NOT_REMINDABLE_STATUSES = ("paid", "void", "disputed")

SKIP_LABELS = {
    "senza_cliente": "Fattura senza cliente",
    "cliente_unito": "Scheda unita a un'altra",
    "cliente_escluso": "Cliente escluso dal recupero",
    "avvocato": "Pratica dall'avvocato",
    "in_incasso": "Assegno in incasso",
    "scadenza_stimata": "Scadenza stimata, non letta dallo scadenzario",
    "forse_pagata": "Forse già pagata (sparita da FatturaPro, in conferma)",
    "abbinamento_da_verificare": "Abbinamento fattura→cliente da verificare (audit)",
    "senza_email": "Cliente senza email",
    "email_non_valida": "Email del cliente non valida",
}

MODE_LABELS = {
    MODE_PROVA: "Prova (nessun invio)",
    MODE_TEST: "Test (tutto a un indirizzo di prova)",
    MODE_REALE: "Reale (ai clienti)",
}

_EMAIL_RE = re.compile(r"^[^\s@,;<>()]+@[^\s@,;<>()]+\.[^\s@,;<>()]+$")

# Un solo giro alla volta: condiviso da job schedulato, pulsante e ritenti.
_reminder_lock = threading.Lock()


class ReminderBusy(Exception):
    """Un giro di promemoria è già in corso."""


class ModeMismatch(Exception):
    """La modalità sul server non è quella che l'operatore ha confermato."""

    def __init__(self, actual: str, expected: str):
        super().__init__(f"modalità sul server: {actual}, confermata: {expected}")
        self.actual = actual
        self.expected = expected


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


def target_due_date(now_utc: Optional[datetime] = None) -> date:
    """La scadenza a cui si riferisce il giro di oggi: DOMANI (ora italiana)."""
    return italian_today(now_utc) + timedelta(days=1)


def in_catchup_window(now_utc: Optional[datetime] = None, start_hour: int = 9, end_hour: int = 18) -> bool:
    """Finestra (ora italiana) in cui un giro di recupero dopo un riavvio è
    accettabile: di sera e di notte non si scrive ai clienti."""
    from zoneinfo import ZoneInfo
    from datetime import timezone
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo(config.TIMEZONE))
    return start_hour <= local.hour < end_hour


def normalize_email(value) -> Optional[str]:
    """Email pulita (minuscole, senza spazi) o None se malformata."""
    key = (value or "").strip().lower()
    return key if key and _EMAIL_RE.match(key) else None


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
    # La scadenza "assumed" è emissione+30 inventata dall'app quando lo
    # scadenzario non la dà: non si può dire al cliente "scade domani".
    if (getattr(invoice, "due_date_source", None) or "assumed") != "real":
        return "scadenza_stimata"
    # Sparita dalla lista «da incassare» di FatturaPro ma non ancora
    # confermata pagata (servono due letture): quasi certamente saldata.
    if (getattr(invoice, "missing_streak", None) or 0) > 0:
        return "forse_pagata"
    # Abbinamento dubbio (stessa regola dell'audit in Sistema): mai mandare
    # la fattura di un altro al cliente sbagliato.
    if getattr(invoice, "audit_reviewed_at", None) is None:
        try:
            verdict = verify_invoice_customer(invoice, customer).get("verdict")
        except Exception:  # la verifica non deve bloccare il giro
            verdict = "warn"
        if verdict != "ok":
            return "abbinamento_da_verificare"
    raw_email = (customer.email or "").strip()
    if not raw_email:
        return "senza_email"
    if normalize_email(raw_email) is None:
        return "email_non_valida"
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
    subject = (
        f"Promemoria di cortesia: fattura n. {invoice.invoice_number} "
        f"in scadenza il {scadenza} ({importo})"
    )

    total = round(sum(float(i.amount_due or 0) for i in overdue), 2)
    paragraphs = [
        f"Spett.le {name},",
        (
            f"Le ricordiamo che la fattura n. {invoice.invoice_number}{emissione}, "
            f"di {importo}, scade domani, {scadenza}.\n"
            "In allegato trova la copia di cortesia della fattura (l'originale Le è stato "
            "trasmesso tramite il Sistema di Interscambio); le coordinate bancarie per il "
            "pagamento sono riportate in calce al documento."
        ),
    ]
    if overdue:
        n = len(overdue)
        scadute = (
            "risulta scaduta e non ancora saldata 1 fattura, per "
            if n == 1 else
            f"risultano scadute e non ancora saldate {n} fatture, per un totale di "
        )
        paragraphs.append(
            f"Le segnaliamo inoltre che, a oggi, {scadute}{fmt_eur(total)}: "
            "nel secondo allegato trova il riepilogo completo. "
            "La preghiamo di provvedere al saldo o di contattarci per concordare le modalità."
        )
    paragraphs.append(
        "Se ha già provveduto al pagamento, La preghiamo di non tenere conto del presente promemoria."
    )
    paragraphs.append("Per qualsiasi chiarimento può rispondere direttamente a questa email.")
    reply_to = (getattr(config, "REMINDER_EMAIL_REPLY_TO", "") or "").strip()
    signature = f"Cordiali saluti,\n{config.COMPANY_NAME} - Amministrazione"
    if reply_to:
        signature += f"\n{reply_to}"
    paragraphs.append(signature)

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
    """Copia di cortesia + (se servono) riepilogo delle scadute. Import locale:
    i generatori vivono in backend.api.recovery (evita cicli di import)."""
    from backend.api.recovery import _build_invoice_pdf, _build_riepilogativo_pdf

    out = [(
        f"copia_cortesia_fattura_{safe_filename(invoice.invoice_number)}.pdf",
        bytes(_build_invoice_pdf(customer, invoice, courtesy=True)),
    )]
    if overdue:
        out.append((
            f"riepilogo_scadute_{safe_filename(customer.ragione_sociale or 'cliente')}.pdf",
            bytes(_build_riepilogativo_pdf(customer, overdue)),
        ))
    return out


# ── Decisioni per riga ────────────────────────────────────────────────

def needs_processing(existing: Optional[InvoiceReminderEmail], mode: str) -> Tuple[bool, str]:
    """Se una riga esistente va (ri)lavorata nel giro di oggi e perché.

    - nessuna riga → sì;
    - skipped → sì (rivalutata: l'email può essere stata aggiunta);
    - failed → sì solo per errori senza invio tentato (PDF, validazione);
    - prepared (prova) → sì se ora la modalità invia davvero;
    - sent in test → sì se ora la modalità è reale (il cliente non l'ha ricevuta);
    - sending (riga orfana di un crash) / sent reale / altro → no.
    """
    if existing is None:
        return True, "nuova"
    st = existing.status
    if st == STATUS_SKIPPED:
        return True, "rivalutazione"
    if st == STATUS_FAILED:
        if (existing.error_kind or ERROR_OTHER) in AUTO_RETRY_KINDS:
            return True, "ritento"
        return False, "fallita: serve «Riprova»"
    if st == STATUS_PREPARED:
        return (mode != MODE_PROVA), ("invio dopo la prova" if mode != MODE_PROVA else "già preparata")
    if st == STATUS_SENT:
        if existing.mode == MODE_TEST and mode == MODE_REALE:
            return True, "invio reale dopo il test"
        return False, "già inviata"
    if st == STATUS_SENDING:
        return False, "invio interrotto: serve «Riprova»"
    return False, st


def _item(row: InvoiceReminderEmail, invoice: Invoice, customer: Optional[Customer]) -> dict:
    return {
        "id": row.id,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "customer_id": customer.id if customer else None,
        "ragione_sociale": customer.ragione_sociale if customer else invoice.customer_name_raw,
        "status": row.status,
        "mode": row.mode,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "skip_reason": row.skip_reason,
        "to_email": row.to_email,
        "amount_due": float(invoice.amount_due or 0),
        "overdue_count": row.overdue_count or 0,
        "overdue_total": float(row.overdue_total or 0),
        "error": row.error,
        "error_kind": row.error_kind,
    }


def preview(session: Session, due_date: Optional[date] = None, now_utc: Optional[datetime] = None,
            mode: Optional[str] = None) -> List[dict]:
    """Cosa farebbe il giro per la scadenza data: NESSUNA scrittura."""
    due = due_date or target_due_date(now_utc)
    mode = mode or resolve_mode()[0]
    out = []
    for inv in candidate_invoices(session, due):
        customer = inv.customer
        existing = session.query(InvoiceReminderEmail).filter_by(invoice_id=inv.id, due_date=due).first()
        reason = skip_reason(inv, customer)
        will, why = needs_processing(existing, mode)
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
            "already_mode": existing.mode if existing else None,
            "will_process": bool(will and not reason),
            "why": why if not reason else SKIP_LABELS.get(reason, reason),
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


# ── Esecuzione ────────────────────────────────────────────────────────

def _resolve_runtime(cfg, mailer, mode):
    """Modalità effettiva + mailer (creato solo fuori dalla prova). Il
    secondo valore ritornato dice se il mailer va chiuso da noi."""
    resolved, missing = resolve_mode(cfg)
    mode = mode or resolved
    if mode not in (MODE_PROVA, MODE_TEST, MODE_REALE):
        raise ValueError(f"modalità sconosciuta: {mode}")
    own_mailer = False
    if mode != MODE_PROVA and mailer is None:
        from backend.connectors.resend_mail import ResendMailer
        mailer = ResendMailer(cfg.RESEND_API_KEY, getattr(cfg, "RESEND_API_URL", None))
        own_mailer = True
    return mode, missing, mailer, own_mailer


def _empty_summary(due: date, mode: str, missing: List[str]) -> dict:
    return {
        "due_date": due.isoformat(), "mode": mode, "missing_config": missing,
        "candidates": 0, "sent": 0, "prepared": 0, "skipped": 0, "failed": 0,
        "already": 0, "items": [],
    }


def _reserve(session: Session, row: InvoiceReminderEmail) -> bool:
    """Salva la riga in stato `sending` PRIMA dell'invio. False se un altro
    giro ha già riservato la stessa (fattura, scadenza)."""
    row.status = STATUS_SENDING
    row.attempted_at = datetime.utcnow()
    session.add(row)
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False


def _classify_error(exc: Exception) -> str:
    kind = getattr(exc, "kind", None)
    return kind if kind in (ERROR_VALIDATION, ERROR_PROVIDER, ERROR_NETWORK) else ERROR_OTHER


def _process_invoice(session: Session, inv: Invoice, due: date, *, mode: str, mailer, cfg, summary: dict,
                     existing: Optional[InvoiceReminderEmail] = None) -> Optional[InvoiceReminderEmail]:
    """Compone, registra e (fuori dalla prova) invia il promemoria di UNA
    fattura; aggiorna `summary` e ritorna la riga (None se un altro giro
    l'aveva già riservata)."""
    customer = inv.customer
    row = existing or InvoiceReminderEmail(invoice_id=inv.id, due_date=due)
    row.customer_id = customer.id if customer else None
    row.mode = mode
    row.amount_due = float(inv.amount_due or 0)
    row.error = None
    row.error_kind = None

    reason = skip_reason(inv, customer)
    if reason:
        row.status = STATUS_SKIPPED
        row.skip_reason = reason
        row.to_email = ((customer.email or "").strip() or None) if customer else None
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            summary["already"] += 1
            return None
        summary["skipped"] += 1
        summary["items"].append(_item(row, inv, customer))
        return row

    overdue = overdue_invoices(session, customer.id, exclude_id=inv.id)
    msg = compose(customer, inv, overdue)
    real_to = normalize_email(customer.email)
    test_to = (getattr(cfg, "REMINDER_EMAIL_TEST_TO", "") or "").strip()
    row.skip_reason = None
    row.subject = msg["subject"]
    row.body_text = msg["text"]
    row.overdue_count = msg["overdue_count"]
    row.overdue_total = msg["overdue_total"]
    row.original_to = real_to
    row.to_email = test_to if mode == MODE_TEST else real_to

    # Riserva PRIMA di fare qualsiasi cosa costosa o irreversibile.
    if not _reserve(session, row):
        summary["already"] += 1
        return None

    try:
        attachments = build_attachments(customer, inv, overdue)
    except Exception as e:  # il PDF non deve far cadere il giro
        logger.error(f"Promemoria {inv.invoice_number}: PDF non generato: {e}", exc_info=True)
        row.status = STATUS_FAILED
        row.error_kind = ERROR_PDF
        row.error = f"PDF non generato: {e}"[:500]
        session.add(row)
        session.commit()
        summary["failed"] += 1
        summary["items"].append(_item(row, inv, customer))
        return row
    row.attachments_json = [name for name, _ in attachments]

    if mode == MODE_PROVA:
        row.status = STATUS_PREPARED
        summary["prepared"] += 1
    else:
        try:
            message_id = mailer.send(
                sender=cfg.REMINDER_EMAIL_FROM,
                to=[row.to_email],
                subject=msg["subject"],
                text=msg["text"],
                html=msg["html"],
                reply_to=(getattr(cfg, "REMINDER_EMAIL_REPLY_TO", "") or None),
                bcc=(getattr(cfg, "REMINDER_EMAIL_BCC", "") or None) if mode == MODE_REALE else None,
                attachments=attachments,
                idempotency_key=f"reminder-{inv.id}-{due.isoformat()}-{mode}",
            )
            row.status = STATUS_SENT
            row.sent_at = datetime.utcnow()
            row.provider_message_id = message_id or None
            summary["sent"] += 1
        except Exception as e:  # messaggio già privo di segreti (MailSendError)
            logger.error(f"Promemoria {inv.invoice_number}: invio fallito: {e}")
            row.status = STATUS_FAILED
            row.error_kind = _classify_error(e)
            row.error = str(e)[:500]
            summary["failed"] += 1
    session.add(row)
    session.commit()
    summary["items"].append(_item(row, inv, customer))
    return row


def _process_safely(session, inv, due, **kwargs) -> None:
    """Una fattura non deve mai far saltare le altre: eccezioni impreviste
    → rollback, riga `failed` (other) se possibile, e si prosegue."""
    summary = kwargs["summary"]
    try:
        _process_invoice(session, inv, due, **kwargs)
    except Exception as e:
        logger.error(f"Promemoria {inv.invoice_number}: errore imprevisto: {e}", exc_info=True)
        try:
            session.rollback()
            row = session.query(InvoiceReminderEmail).filter_by(invoice_id=inv.id, due_date=due).first()
            if row is None:
                row = InvoiceReminderEmail(invoice_id=inv.id, due_date=due, customer_id=inv.customer_id,
                                           mode=kwargs["mode"], amount_due=float(inv.amount_due or 0))
            row.status = STATUS_FAILED
            row.error_kind = ERROR_OTHER
            row.error = f"Errore imprevisto: {e}"[:500]
            session.add(row)
            session.commit()
            summary["failed"] += 1
            summary["items"].append(_item(row, inv, inv.customer))
        except Exception as inner:
            logger.error(f"Promemoria {inv.invoice_number}: impossibile registrare l'errore: {inner}")
            try:
                session.rollback()
            except Exception:
                pass


def run_reminders(
    session: Session,
    *,
    now_utc: Optional[datetime] = None,
    due_date: Optional[date] = None,
    mailer=None,
    cfg=None,
    mode: Optional[str] = None,
    expected_mode: Optional[str] = None,
) -> dict:
    """Il giro quotidiano: compone, registra e (fuori dalla prova) invia.

    `expected_mode`: la modalità che l'operatore ha confermato a video; se il
    server ne risolve un'altra (env cambiate nel frattempo) non si parte.
    """
    cfg = cfg or config
    due = due_date or target_due_date(now_utc)
    if not _reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, missing, mailer, own_mailer = _resolve_runtime(cfg, mailer, mode)
        if expected_mode and expected_mode != mode:
            raise ModeMismatch(mode, expected_mode)
        summary = _empty_summary(due, mode, missing)
        try:
            for inv in candidate_invoices(session, due):
                summary["candidates"] += 1
                existing = session.query(InvoiceReminderEmail).filter_by(invoice_id=inv.id, due_date=due).first()
                will, _why = needs_processing(existing, mode)
                if not will:
                    summary["already"] += 1
                    continue
                _process_safely(session, inv, due, mode=mode, mailer=mailer, cfg=cfg, summary=summary,
                                existing=existing)
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        _reminder_lock.release()

    logger.info(
        "Promemoria email (scadenza %s, modalità %s): candidate=%d inviate=%d preparate=%d "
        "saltate=%d fallite=%d non_lavorate=%d",
        summary["due_date"], mode, summary["candidates"], summary["sent"], summary["prepared"],
        summary["skipped"], summary["failed"], summary["already"],
    )
    return summary


def retry_row(session: Session, row: InvoiceReminderEmail, *, mailer=None, cfg=None, mode: Optional[str] = None,
              expected_mode: Optional[str] = None) -> dict:
    """Ritenta UNA riga ferma (`failed` o `sending` orfana), anche di una
    scadenza passata: il giro quotidiano guarda solo la scadenza di domani.
    La fattura deve essere ancora da ricordare."""
    cfg = cfg or config
    if row.status not in (STATUS_FAILED, STATUS_SENDING):
        raise ValueError("si ritentano solo i promemoria falliti o interrotti")
    inv = session.query(Invoice).filter(Invoice.id == row.invoice_id).first()
    if inv is None:
        raise ValueError("fattura non più presente")
    if inv.status in NOT_REMINDABLE_STATUSES or float(inv.amount_due or 0) <= 0:
        raise ValueError("fattura non più da ricordare (pagata, annullata o contestata)")
    if not _reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, missing, mailer, own_mailer = _resolve_runtime(cfg, mailer, mode)
        if expected_mode and expected_mode != mode:
            raise ModeMismatch(mode, expected_mode)
        summary = _empty_summary(row.due_date, mode, missing)
        summary["candidates"] = 1
        try:
            _process_safely(session, inv, row.due_date, mode=mode, mailer=mailer, cfg=cfg, summary=summary,
                            existing=row)
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        _reminder_lock.release()
    return summary
