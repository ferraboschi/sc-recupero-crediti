"""Promemoria email il giorno PRIMA della scadenza di una fattura.

Decisione owner (9/11/2026): per ogni fattura non pagata in scadenza domani
(il venerdì: sabato, domenica e lunedì; nel weekend nulla) il cliente riceve
una email con:
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
from backend.engine.agents import agents_of
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


class NotFound(LookupError):
    """Riga o fattura inesistente."""


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


def target_due_dates(now_utc: Optional[datetime] = None) -> List[date]:
    """Le scadenze a cui si riferisce il giro di oggi (ora italiana).

    Decisione owner (10/11/2026): mai email nel weekend. Da lunedì a giovedì
    si ricorda la scadenza di DOMANI; il venerdì si anticipano sabato,
    domenica e lunedì; sabato e domenica il giro non fa nulla.
    """
    today = italian_today(now_utc)
    weekday = today.weekday()  # 0 = lunedì
    if weekday >= 5:
        return []
    if weekday == 4:
        return [today + timedelta(days=d) for d in (1, 2, 3)]
    return [today + timedelta(days=1)]


def target_due_date(now_utc: Optional[datetime] = None) -> date:
    """Compatibilità: la prima scadenza del giro (o domani, nel weekend)."""
    dates = target_due_dates(now_utc)
    return dates[0] if dates else italian_today(now_utc) + timedelta(days=1)


WEEKDAYS_IT = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")


def due_label(due: date, today: Optional[date] = None) -> str:
    """«domani, martedì 11/11/2026» oppure «lunedì 16/11/2026» (giro del venerdì)."""
    today = today or italian_today()
    text = f"{WEEKDAYS_IT[due.weekday()]} {fmt_date(due)}"
    return f"domani, {text}" if due == today + timedelta(days=1) else text


_NUMBER_SUFFIX_RE = re.compile(r"\s*-\s*(fattura|nota di credito|nota di debito)\s*$", re.IGNORECASE)


def invoice_label(invoice_number) -> str:
    """Numero come lo legge il cliente: FatturaPro accoda « - Fattura» al
    numero («2026/00001804/SAK - Fattura»); nell'email e nella causale resta
    solo «2026/00001804/SAK»."""
    return _NUMBER_SUFFIX_RE.sub("", str(invoice_number or "")).strip() or str(invoice_number or "")


def agent_emails(cfg=None) -> dict:
    """{'Maggioni': 'mail', ...} da REMINDER_AGENT_EMAILS ("Nome:mail;Nome:mail")."""
    raw = (getattr(cfg or config, "REMINDER_AGENT_EMAILS", "") or "").strip()
    out = {}
    for part in raw.replace(";", ",").split(","):
        if ":" in part or "=" in part:
            name, _, mail = part.replace("=", ":").partition(":")
            mail = normalize_email(mail)
            if name.strip() and mail:
                out[name.strip().title()] = mail
    return out


def carbon_copies(customer: Optional[Customer], cfg=None) -> List[str]:
    """Copie della email REALE: casella fatture (REMINDER_EMAIL_CC) e il
    commerciale di riferimento del cliente (tag Shopify → REMINDER_AGENT_EMAILS)."""
    cfg = cfg or config
    cc: List[str] = []
    mapping = agent_emails(cfg)
    for agent in agents_of(customer):
        mail = mapping.get(agent)
        if mail and mail not in cc:
            cc.append(mail)
    for part in (getattr(cfg, "REMINDER_EMAIL_CC", "") or "").replace(";", ",").split(","):
        mail = normalize_email(part)
        if mail and mail not in cc:
            cc.append(mail)
    to = normalize_email(getattr(customer, "email", None)) if customer else None
    return [m for m in cc if m != to]


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

def compose(customer: Customer, invoice: Invoice, overdue: List[Invoice], today: Optional[date] = None) -> dict:
    """Oggetto e testo (testo semplice + HTML). Decisione owner (10/11/2026):
    segnalazione corta e veloce ma con tutti i dettagli — fattura, importo,
    scadenza, allegati, eventuali scadute col totale, coordinate di pagamento,
    cosa fare se ha già pagato."""
    name = (customer.ragione_sociale or "").strip() or "Cliente"
    importo = fmt_eur(invoice.amount_due)
    scadenza = fmt_date(invoice.due_date)
    label = due_label(invoice.due_date, today) if invoice.due_date else "a breve"
    numero = invoice_label(invoice.invoice_number)
    subject = f"Promemoria: fattura n. {numero} in scadenza il {scadenza} ({importo})"

    total = round(sum(float(i.amount_due or 0) for i in overdue), 2)
    paragraphs = [
        f"Spett.le {name},",
        (
            f"promemoria di cortesia: la fattura n. {numero} di {importo} "
            f"scade {label}. Copia della fattura in allegato."
        ),
    ]
    if overdue:
        n = len(overdue)
        if n == 1:
            paragraphs.append(
                f"Risulta inoltre scaduta e non saldata 1 fattura per {fmt_eur(total)}: riepilogo in allegato."
            )
        else:
            paragraphs.append(
                f"Risultano inoltre scadute e non saldate {n} fatture per un totale di {fmt_eur(total)}: "
                "riepilogo in allegato."
            )
    pagamento = f"Pagamento: bonifico a {config.COMPANY_NAME}"
    if config.COMPANY_IBAN:
        pagamento += f", IBAN {config.COMPANY_IBAN}"
    pagamento += f", causale \"Fattura {numero}\"."
    paragraphs.append(pagamento)
    paragraphs.append(
        "Se ha già provveduto al pagamento, ignori questo messaggio. "
        "Per chiarimenti può rispondere a questa email."
    )
    paragraphs.append(f"{config.COMPANY_NAME} - Amministrazione")

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
        f"copia_cortesia_fattura_{safe_filename(invoice_label(invoice.invoice_number))}.pdf",
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
        "cc": row.cc_json or [],
        "amount_due": float(invoice.amount_due or 0),
        "overdue_count": row.overdue_count or 0,
        "overdue_total": float(row.overdue_total or 0),
        "error": row.error,
        "error_kind": row.error_kind,
    }


def preview(session: Session, due_date: Optional[date] = None, now_utc: Optional[datetime] = None,
            mode: Optional[str] = None, due_dates: Optional[List[date]] = None, cfg=None) -> List[dict]:
    """Cosa farebbe il giro per le scadenze date: NESSUNA scrittura."""
    cfg = cfg or config
    dates = due_dates or ([due_date] if due_date else target_due_dates(now_utc))
    mode = mode or resolve_mode(cfg)[0]
    today = italian_today(now_utc)
    out = []
    for due in dates:
        out.extend(_preview_for(session, due, mode, today, cfg))
    return out


def _preview_for(session: Session, due: date, mode: str, today: date, cfg) -> List[dict]:
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
            "cc": carbon_copies(customer, cfg) if (customer and mode == MODE_REALE) else [],
            "would_cc": carbon_copies(customer, cfg) if customer else [],
            "amount_due": float(inv.amount_due or 0),
            "due_date": due.isoformat(),
            "due_label": due_label(due, today),
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
            msg = compose(customer, inv, overdue, today)
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


def _empty_summary(due: Optional[date], mode: str, missing: List[str]) -> dict:
    return {
        "due_date": due.isoformat() if due else None, "mode": mode, "missing_config": missing,
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
                     existing: Optional[InvoiceReminderEmail] = None,
                     today: Optional[date] = None) -> Optional[InvoiceReminderEmail]:
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
    msg = compose(customer, inv, overdue, today or italian_today())
    real_to = normalize_email(customer.email)
    test_to = (getattr(cfg, "REMINDER_EMAIL_TEST_TO", "") or "").strip()
    row.skip_reason = None
    row.subject = msg["subject"]
    row.body_text = msg["text"]
    row.overdue_count = msg["overdue_count"]
    row.overdue_total = msg["overdue_total"]
    row.original_to = real_to
    row.to_email = test_to if mode == MODE_TEST else real_to
    # Copie (commerciale + fatture@) solo nell'invio REALE: in test tutto va
    # al solo indirizzo di prova.
    cc = carbon_copies(customer, cfg) if mode == MODE_REALE else []
    row.cc_json = cc

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
                cc=cc or None,
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
    due_dates: Optional[List[date]] = None,
) -> dict:
    """Il giro quotidiano: compone, registra e (fuori dalla prova) invia.

    `expected_mode`: la modalità che l'operatore ha confermato a video; se il
    server ne risolve un'altra (env cambiate nel frattempo) non si parte.
    """
    cfg = cfg or config
    if due_dates is None:
        dates = [due_date] if due_date else target_due_dates(now_utc)
    else:
        dates = list(due_dates)
    if not _reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, missing, mailer, own_mailer = _resolve_runtime(cfg, mailer, mode)
        if expected_mode and expected_mode != mode:
            raise ModeMismatch(mode, expected_mode)
        summary = _empty_summary(dates[0] if dates else None, mode, missing)
        summary["due_dates"] = [d.isoformat() for d in dates]
        today = italian_today(now_utc)
        try:
            for due in dates:
                for inv in candidate_invoices(session, due):
                    summary["candidates"] += 1
                    existing = session.query(InvoiceReminderEmail).filter_by(invoice_id=inv.id, due_date=due).first()
                    will, _why = needs_processing(existing, mode)
                    if not will:
                        summary["already"] += 1
                        continue
                    _process_safely(session, inv, due, mode=mode, mailer=mailer, cfg=cfg, summary=summary,
                                    existing=existing, today=today)
            # Passaggio prova/test → reale: le scadenze FUTURE già lavorate in
            # prova o in test (es. il venerdì copre anche lunedì) non devono
            # restare senza l'email vera. Si riprendono le righe con
            # scadenza da oggi in poi, fuori dalle date del giro.
            if mode == MODE_REALE:
                pending = (
                    session.query(InvoiceReminderEmail)
                    .filter(
                        InvoiceReminderEmail.due_date >= today,
                        InvoiceReminderEmail.status.in_((STATUS_PREPARED, STATUS_SENT)),
                        InvoiceReminderEmail.mode != MODE_REALE,
                    )
                    .all()
                )
                for row in pending:
                    if row.due_date in dates:
                        continue
                    will, _why = needs_processing(row, mode)
                    inv = session.query(Invoice).filter(Invoice.id == row.invoice_id).first()
                    if not will or inv is None or inv.status in NOT_REMINDABLE_STATUSES or float(inv.amount_due or 0) <= 0:
                        continue
                    summary["candidates"] += 1
                    summary.setdefault("carried_over", 0)
                    summary["carried_over"] += 1
                    _process_safely(session, inv, row.due_date, mode=mode, mailer=mailer, cfg=cfg,
                                    summary=summary, existing=row, today=today)
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        _reminder_lock.release()

    logger.info(
        "Promemoria email (scadenze %s, modalità %s): candidate=%d inviate=%d preparate=%d "
        "saltate=%d fallite=%d non_lavorate=%d",
        ", ".join(summary["due_dates"]) or "nessuna (weekend)", mode, summary["candidates"], summary["sent"],
        summary["prepared"], summary["skipped"], summary["failed"], summary["already"],
    )
    return summary


def send_test(session: Session, invoice_id: int, to_address: str, *, mailer=None, cfg=None) -> dict:
    """Invia a UN indirizzo scelto l'email che il cliente riceverebbe per la
    fattura data (oggetto con prefisso [PROVA], nessuna copia, registro
    intoccato). Serve a far leggere testo e allegati veri prima di accendere
    l'invio ai clienti. Richiede Resend configurato."""
    from sqlalchemy import func

    cfg = cfg or config
    to = normalize_email(to_address)
    if not to:
        raise ValueError("indirizzo di prova non valido")
    inv = session.query(Invoice).filter(Invoice.id == invoice_id).first()
    if inv is None:
        raise NotFound("fattura non trovata")
    if inv.customer is None:
        raise ValueError("fattura senza cliente")
    if inv.status in NOT_REMINDABLE_STATUSES or float(inv.amount_due or 0) <= 0:
        raise ValueError("fattura pagata, annullata o contestata")
    customer = inv.customer
    reason = skip_reason(inv, customer)
    if reason and reason not in ("senza_email", "email_non_valida"):
        raise ValueError(f"promemoria non inviabile: {SKIP_LABELS.get(reason, reason)}")
    # La prova va a un indirizzo INTERNO: mai a un cliente (nemmeno per errore
    # di copia-incolla dall'anteprima).
    is_customer_email = session.query(Customer.id).filter(
        func.lower(func.trim(Customer.email)) == to
    ).first() is not None
    if is_customer_email or to == normalize_email(customer.email):
        raise ValueError("l'indirizzo di prova coincide con l'email di un cliente: usare un indirizzo interno")
    if not getattr(cfg, "RESEND_API_KEY", ""):
        raise ValueError("Resend non configurato (RESEND_API_KEY)")
    overdue = overdue_invoices(session, customer.id, exclude_id=inv.id)
    msg = compose(customer, inv, overdue, italian_today())
    attachments = build_attachments(customer, inv, overdue)
    own = False
    if mailer is None:
        from backend.connectors.resend_mail import ResendMailer
        mailer = ResendMailer(cfg.RESEND_API_KEY, getattr(cfg, "RESEND_API_URL", None))
        own = True
    try:
        message_id = mailer.send(
            sender=cfg.REMINDER_EMAIL_FROM,
            to=[to],
            subject=f"[PROVA] {msg['subject']}",
            text=msg["text"],
            html=msg["html"],
            reply_to=(getattr(cfg, "REMINDER_EMAIL_REPLY_TO", "") or None),
            attachments=attachments,
        )
    finally:
        if own:
            mailer.close()
    logger.info("Promemoria di PROVA inviato per la fattura %s (cliente %s)", inv.invoice_number, customer.id)
    return {
        "sent_to": to, "invoice_id": inv.id, "invoice_number": inv.invoice_number,
        "ragione_sociale": customer.ragione_sociale, "subject": f"[PROVA] {msg['subject']}",
        "attachments": [name for name, _ in attachments], "provider_message_id": message_id or None,
        "would_cc": carbon_copies(customer, cfg),
    }


def retry_row(session: Session, row: InvoiceReminderEmail, *, mailer=None, cfg=None, mode: Optional[str] = None,
              expected_mode: Optional[str] = None) -> dict:
    """Ritenta UNA riga ferma (`failed` o `sending` orfana) di una scadenza
    non ancora passata: il giro quotidiano guarda solo le prossime scadenze.
    La fattura deve essere ancora da ricordare."""
    cfg = cfg or config
    if row.status not in (STATUS_FAILED, STATUS_SENDING):
        raise ValueError("si ritentano solo i promemoria falliti o interrotti")
    if row.due_date and row.due_date < italian_today():
        raise ValueError("scadenza già passata: non è più un promemoria (la fattura è ora tra le scadute)")
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
