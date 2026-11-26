"""Promemoria email il giorno PRIMA della scadenza di una fattura.

Decisione owner (9/11/2026): per ogni fattura non pagata in scadenza domani
(il venerdì: sabato, domenica e lunedì; nel weekend nulla) il cliente riceve
una email con:
- la copia di cortesia della fattura in scadenza (PDF generato dall'app);
- se ha anche fatture GIÀ scadute e non saldate, un secondo PDF con il
  riepilogo e il totale, citato nel testo ("questa fattura vale X; il
  riepilogo delle scadute a oggi vale Y").
Una email per CLIENTE (decisione owner 26/11/2026: «non si devono ricevere
due mail; in caso di scadenze prossime unire le mail e adeguare il testo;
scadenza prossima significa ± un giorno»): tutte le fatture di un cliente
che il giro di oggi deve ricordare partono in UN'UNICA email (elenco, totale,
una copia di cortesia per ciascuna). Nella stessa email entra anche una sua
fattura che scade il giorno dopo l'ultima scadenza del giro: così due
scadenze a un giorno di distanza non producono due email in due giorni. Da
sola, quella fattura non fa partire nulla: aspetta il suo giorno. Con una
sola fattura l'email è quella di sempre, parola per parola.

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
  secondo trova il vincolo e passa oltre. Le righe della stessa email
  condividono `group_key`, vengono riservate insieme e hanno sempre lo
  stesso esito;
- Idempotency-Key sulla chiamata a Resend — (fattura, scadenza, modalità)
  per un'email con una fattura, (cliente, giorno, fatture, modalità) per
  un'email con più fatture — salvata sulle righe: un ritento dopo un
  timeout riusa la chiave del tentativo incerto, qualunque siano oggi le
  fatture, e non duplica l'email;
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
import hashlib
import html
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

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
# Resend risponde 409 quando la chiave del tentativo è già stata usata con un
# contenuto diverso: un tentativo PRECEDENTE gli era arrivato. È il contrario
# di «certamente non partita»: mai un ritento automatico.
ERROR_CONFLICT = "conflict"
AUTO_RETRY_KINDS = (ERROR_PDF, ERROR_VALIDATION)

# Resend ricorda una chiave per 24 ore: oltre non ferma più un doppione. Un
# margine di un'ora, contato dalla NASCITA della riga (il primo uso possibile
# della chiave: mai dopo il primo tentativo).
RESEND_KEY_MEMORY = timedelta(hours=23)
CONFLICT_NOTE = (
    " — Resend aveva già ricevuto un tentativo precedente di questa email: prima di ritentare, "
    "controllare su Resend se è stata consegnata."
)
CONFIRMATION_NEEDED = (
    "questo promemoria potrebbe essere già arrivato al cliente e Resend potrebbe non fermare più un doppione: "
    "si invia solo confermando di aver controllato su Resend che NON è stato consegnato"
)
REAL_DOUBT_ONLY_IN_REAL = (
    "questo promemoria era un invio REALE dall'esito incerto: si ritenta solo in modalità reale"
)

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
    # Solo secondo avviso (backend/engine/followup_email.py):
    "senza_promemoria": "Promemoria pre-scadenza non inviato al cliente",
    "in_attesa": "Pratica in attesa (decisa dall'operatore)",
    "promessa": "Pagamento promesso (stato impostato dall'operatore)",
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


def overdue_invoices(session: Session, customer_id: int, exclude_id: Optional[int] = None,
                     exclude_ids: Optional[Iterable[int]] = None) -> List[Invoice]:
    """Le scadute e non saldate del cliente (stessa definizione della cascata:
    non pagate/annullate/contestate, giorni di ritardo > 0, niente assegni in
    incasso), escludendo la fattura (o le fatture) oggetto del promemoria."""
    query = session.query(Invoice).filter(
        Invoice.customer_id == customer_id,
        Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
        Invoice.days_overdue > 0,
        Invoice.amount_due > 0,
    )
    excluded = ([exclude_id] if exclude_id is not None else []) + list(exclude_ids or [])
    if len(excluded) == 1:
        query = query.filter(Invoice.id != excluded[0])
    elif excluded:
        query = query.filter(Invoice.id.notin_(excluded))
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
    grand_total = round(float(invoice.amount_due or 0) + total, 2)
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
            scadute = f"Risulta inoltre scaduta e non saldata 1 fattura per {fmt_eur(total)}."
        else:
            scadute = f"Risultano inoltre scadute e non saldate {n} fatture per un totale di {fmt_eur(total)}."
        paragraphs.append(
            f"{scadute} Totale da saldare, compresa la fattura in scadenza: {fmt_eur(grand_total)}. "
            "Riepilogo in allegato."
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
        "grand_total": grand_total,
    }


# Oltre questo numero di fatture la causale non le elenca più tutte.
MAX_NUMBERS_IN_CAUSALE = 3


def join_it(items) -> str:
    """«A», «A e B», «A, B e C»."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " e " + items[-1]


def in_email_order(invoices) -> List[Invoice]:
    """Dalla scadenza più vicina, poi per numero: l'ordine dell'elenco."""
    return sorted(invoices, key=lambda i: (i.due_date or date.max, str(i.invoice_number or ""), i.id or 0))


def compose_many(customer: Customer, invoices: List[Invoice], overdue: List[Invoice],
                 today: Optional[date] = None) -> dict:
    """Oggetto e testo di UN promemoria per una o più fatture in scadenza
    dello stesso cliente (decisione owner 26/11/2026: una email per cliente;
    le scadenze a un giorno di distanza si uniscono). Con una sola fattura è
    il testo di sempre, parola per parola (`compose`). Con più fatture:
    l'elenco con importo e scadenza di ciascuna, il loro totale, le eventuali
    scadute col totale complessivo. `overdue`: le fatture GIÀ scadute del
    cliente, non elencate qui."""
    invoices = in_email_order(invoices)
    if not invoices:
        raise ValueError("nessuna fattura da ricordare")
    if len(invoices) == 1:
        msg = compose(customer, invoices[0], overdue, today)
        msg.update({"invoice_count": 1, "invoices_total": round(float(invoices[0].amount_due or 0), 2)})
        return msg
    name = (customer.ragione_sociale or "").strip() or "Cliente"
    numbers = [invoice_label(i.invoice_number) for i in invoices]
    invoices_total = round(sum(float(i.amount_due or 0) for i in invoices), 2)
    total = round(sum(float(i.amount_due or 0) for i in overdue), 2)
    grand_total = round(invoices_total + total, 2)
    dues = {i.due_date for i in invoices}
    if len(dues) == 1 and None not in dues:
        subject = (f"Promemoria: {len(invoices)} fatture in scadenza il {fmt_date(invoices[0].due_date)} "
                   f"({fmt_eur(invoices_total)})")
    else:
        subject = f"Promemoria: {len(invoices)} fatture in scadenza ({fmt_eur(invoices_total)})"

    def riga(inv: Invoice, numero: str) -> str:
        quando = f"scade {due_label(inv.due_date, today)}" if inv.due_date else "in scadenza a breve"
        return f"- n. {numero} di {fmt_eur(inv.amount_due)}, {quando}"

    elenco = "\n".join(riga(inv, numero) for inv, numero in zip(invoices, numbers))
    paragraphs = [
        f"Spett.le {name},",
        "promemoria di cortesia: sono in scadenza le seguenti fatture:\n" + elenco,
    ]
    # Con delle scadute il totale complessivo arriva nel paragrafo dopo: qui
    # si dice di che cosa è il totale.
    etichetta = "Totale di queste fatture" if overdue else "Totale"
    paragraphs.append(f"{etichetta}: {fmt_eur(invoices_total)}. Copie delle fatture in allegato.")
    if overdue:
        n = len(overdue)
        if n == 1:
            scadute = f"Risulta inoltre scaduta e non saldata 1 fattura per {fmt_eur(total)}."
        else:
            scadute = f"Risultano inoltre scadute e non saldate {n} fatture per un totale di {fmt_eur(total)}."
        paragraphs.append(
            f"{scadute} Totale da saldare, comprese le fatture in scadenza: {fmt_eur(grand_total)}. "
            "Riepilogo in allegato."
        )
    pagamento = f"Pagamento: bonifico a {config.COMPANY_NAME}"
    if config.COMPANY_IBAN:
        pagamento += f", IBAN {config.COMPANY_IBAN}"
    distinct = list(dict.fromkeys(numbers))  # due documenti con lo stesso numero: una volta sola
    if len(distinct) == 1:
        pagamento += f", causale \"Fattura {distinct[0]}\"."
    elif len(distinct) <= MAX_NUMBERS_IN_CAUSALE:
        pagamento += f", causale \"Fatture {join_it(distinct)}\"."
    else:
        pagamento += ", indicando nella causale i numeri delle fatture."
    paragraphs.append(pagamento)
    paragraphs.append(
        "Se ha già provveduto al pagamento, ignori questo messaggio. "
        "Per chiarimenti può rispondere a questa email."
    )
    paragraphs.append(f"{config.COMPANY_NAME} - Amministrazione")

    text = "\n\n".join(paragraphs)
    html_body = "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)
    return {
        "subject": subject,
        "text": text,
        "html": html_body,
        "invoice_count": len(invoices),
        "invoices_total": invoices_total,
        "overdue_count": len(overdue),
        "overdue_total": total,
        "grand_total": grand_total,
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
        # Decisione owner (10/11/2026): nel riepilogo anche la fattura in
        # scadenza che ha generato l'email (in cima), poi le scadute coi
        # giorni di ritardo, e il totale di tutte: si aspetta il saldo di
        # entrambe.
        out.append((
            f"riepilogo_da_saldare_{safe_filename(customer.ragione_sociale or 'cliente')}.pdf",
            bytes(_build_riepilogativo_pdf(customer, [invoice] + list(overdue), title="Riepilogo Fatture da Saldare")),
        ))
    return out


def unique_names(names: List[str], ids: List[int]) -> List[str]:
    """Nomi di file senza doppioni. Due documenti con lo stesso numero
    visibile (fattura e nota di debito) prendono ENTRAMBI l'id del documento
    in coda: così il nome non dipende dall'ordine in cui compaiono."""
    counts: Dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    out: List[str] = []
    for name, ident in zip(names, ids):
        if counts[name] > 1:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem}_{ident}.{ext}" if dot else f"{name}_{ident}"
        candidate, n = name, 1
        while candidate in out:  # un nome già «con l'id» di un altro documento: caso di scuola
            n += 1
            stem, dot, ext = name.rpartition(".")
            candidate = f"{stem}_{n}.{ext}" if dot else f"{name}_{n}"
        out.append(candidate)
    return out


def build_group_attachments(customer: Customer, invoices: List[Invoice], overdue: List[Invoice]) -> List[Tuple[str, bytes]]:
    """Gli allegati di UN promemoria. Una fattura sola: quelli di sempre
    (`build_attachments`). Più fatture: una copia di cortesia per ciascuna,
    nell'ordine dell'elenco, + (se il cliente ha delle scadute) il riepilogo
    con le fatture in scadenza in cima, poi le scadute, e il totale di tutte."""
    invoices = in_email_order(invoices)
    if len(invoices) == 1:
        return build_attachments(customer, invoices[0], overdue)
    from backend.api.recovery import _build_invoice_pdf, _build_riepilogativo_pdf

    names = unique_names(
        [f"copia_cortesia_fattura_{safe_filename(invoice_label(inv.invoice_number))}.pdf" for inv in invoices],
        [inv.id for inv in invoices],
    )
    out = [(name, bytes(_build_invoice_pdf(customer, inv, courtesy=True))) for name, inv in zip(names, invoices)]
    if overdue:
        out.append((
            f"riepilogo_da_saldare_{safe_filename(customer.ragione_sociale or 'cliente')}.pdf",
            bytes(_build_riepilogativo_pdf(customer, list(invoices) + list(overdue), title="Riepilogo Fatture da Saldare")),
        ))
    return out


# ── Una email per cliente ─────────────────────────────────────────────

# «Scadenza prossima» = ± un giorno (decisione owner 24/11/2026).
NEAR_DUE_DAYS = 1


def near_due_date(dates: Iterable[date]) -> Optional[date]:
    """La scadenza che si può UNIRE alle email del giro: il giorno dopo
    l'ultima scadenza del giro. Da sola non fa partire nulla."""
    dates = list(dates)
    return max(dates) + timedelta(days=NEAR_DUE_DAYS) if dates else None


def by_customer(invoices: Iterable[Invoice]) -> List[Tuple[int, List[Invoice]]]:
    """Le fatture raggruppate per cliente — [(id cliente, fatture)] —
    nell'ordine in cui il primo cliente compare."""
    groups: Dict[int, List[Invoice]] = {}
    for inv in invoices:
        groups.setdefault(inv.customer_id, []).append(inv)
    return list(groups.items())


def group_key(customer_id: int, invoices: Iterable[Invoice], today: date) -> str:
    """Identità di UNA email nel registro: cliente, giorno, fatture (con la
    scadenza). Serve a dire quali righe hanno viaggiato insieme."""
    parts = ",".join(
        f"{inv.id}:{inv.due_date.isoformat() if inv.due_date else ''}"
        for inv in sorted(invoices, key=lambda i: i.id)
    )
    digest = hashlib.sha256(f"{customer_id}|{parts}".encode("utf-8")).hexdigest()[:16]
    return f"c{customer_id}-{today.strftime('%Y%m%d')}-{digest}"


def attempt_key(invoices: List[Invoice], mode: str, key: str) -> str:
    """La chiave verso Resend di un tentativo. Una fattura sola: la chiave di
    sempre (fattura, scadenza, modalità), identica a prima dell'email unica —
    così il ritento di una riga nata allora resta protetto. Più fatture: la
    chiave dell'email (`key` = group_key: cliente, giorno, fatture)."""
    if len(invoices) == 1:
        inv = invoices[0]
        return f"reminder-{inv.id}-{inv.due_date.isoformat()}-{mode}"
    return f"reminder-{key}-{mode}"


def group_members(session: Session, rows: Iterable[InvoiceReminderEmail]) -> Dict[str, List[dict]]:
    """Per ogni `group_key` delle righe date, le fatture della stessa email
    (dalla scadenza più vicina): {chiave: [{row_id, invoice_id, invoice_number, due_date, amount_due}]}."""
    keys = {r.group_key for r in rows if getattr(r, "group_key", None)}
    out: Dict[str, List[dict]] = {}
    if not keys:
        return out
    found = (
        session.query(InvoiceReminderEmail, Invoice)
        .outerjoin(Invoice, Invoice.id == InvoiceReminderEmail.invoice_id)
        .filter(InvoiceReminderEmail.group_key.in_(keys))
        .order_by(InvoiceReminderEmail.due_date.asc(), InvoiceReminderEmail.id.asc())
        .all()
    )
    for row, inv in found:
        out.setdefault(row.group_key, []).append({
            "row_id": row.id,
            "invoice_id": row.invoice_id,
            "invoice_number": inv.invoice_number if inv else None,
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "amount_due": float(row.amount_due or 0),
        })
    return out


def row_invoices(session: Session, row: InvoiceReminderEmail) -> List[Invoice]:
    """Le fatture dell'email a cui appartiene la riga (lei compresa), per
    rigenerare gli allegati: quelle ancora presenti nel DB."""
    ids = {row.invoice_id}
    if row.group_key:
        ids |= {
            r.invoice_id for r in
            session.query(InvoiceReminderEmail).filter(InvoiceReminderEmail.group_key == row.group_key).all()
        }
    return in_email_order(session.query(Invoice).filter(Invoice.id.in_(ids)).all())


def _existing_row(session: Session, invoice: Invoice) -> Optional[InvoiceReminderEmail]:
    return (
        session.query(InvoiceReminderEmail)
        .filter_by(invoice_id=invoice.id, due_date=invoice.due_date)
        .first()
    )


@dataclass
class CustomerPlan:
    """Cosa fa il giro di oggi per le fatture di UN cliente."""
    customer: Optional[Customer] = None
    # Le fatture dell'UNICA email del cliente (vuoto = nessuna email oggi),
    # nell'ordine dell'elenco.
    email: List[Invoice] = field(default_factory=list)
    # Tra quelle, le fatture UNITE perché scadono il giorno dopo le scadenze del giro.
    early_ids: Set[int] = field(default_factory=set)
    # Righe già presenti nel registro, per fattura.
    existing: Dict[int, InvoiceReminderEmail] = field(default_factory=dict)
    # Fatture del giro escluse, col motivo.
    skipped: List[Tuple[Invoice, str]] = field(default_factory=list)
    # Fatture del giro con una riga che oggi non si rilavora (già inviata, ferma…).
    already: List[Tuple[Invoice, InvoiceReminderEmail, str]] = field(default_factory=list)
    # Fatture con una scadenza del giro (le «candidate»).
    candidates: int = 0


def plan_customer(session: Session, invoices: Iterable[Invoice], targets: Iterable[date], mode: str, *,
                  customer_id: Optional[int] = None, ignore_email: bool = False) -> CustomerPlan:
    """Decide, per le fatture candidate di UN cliente, cosa succede oggi.

    Una fattura è PRONTA se è ancora da saldare, non ha una riga che blocca
    (già inviata, ferma…) e non ha motivi di esclusione. L'email parte se
    almeno una pronta ha una scadenza DEL GIRO; in quel caso porta con sé
    anche le pronte che scadono il giorno dopo. Queste, da sole, non
    producono nulla oggi: né email, né righe, né conteggi.

    Lo stato delle fatture è quello letto ADESSO: una fattura saldata, o
    passata a un altro cliente, a giro in corso esce dal piano.
    `ignore_email`: l'invio di prova va a un indirizzo interno, quindi
    l'email del cliente non serve.
    """
    targets = set(targets)
    near = near_due_date(targets)
    invoices = in_email_order(invoices)
    if customer_id is None and invoices:
        customer_id = invoices[0].customer_id
    plan = CustomerPlan()
    ready: List[Tuple[Invoice, bool]] = []
    for inv in invoices:
        if inv.customer_id is None or inv.customer_id != customer_id:
            continue
        early = inv.due_date not in targets
        if early and inv.due_date != near:
            continue
        if inv.status in NOT_REMINDABLE_STATUSES or float(inv.amount_due or 0) <= 0:
            continue
        if plan.customer is None:
            plan.customer = inv.customer
        row = _existing_row(session, inv)
        if row is not None:
            plan.existing[inv.id] = row
        if not early:
            plan.candidates += 1
        will, why = needs_processing(row, mode)
        if not will:
            if not early:
                plan.already.append((inv, row, why))
            continue
        reason = skip_reason(inv, inv.customer)
        if ignore_email and reason in ("senza_email", "email_non_valida"):
            reason = None
        if reason:
            if not early:
                plan.skipped.append((inv, reason))
            continue
        ready.append((inv, early))
    if any(not early for _inv, early in ready):
        plan.email = [inv for inv, _early in ready]
        plan.early_ids = {inv.id for inv, early in ready if early}
    return plan


# ── Decisioni per riga ────────────────────────────────────────────────

def is_conflict(exc: Exception) -> bool:
    """Resend ha risposto «chiave già usata» (HTTP 409)?"""
    return getattr(exc, "status_code", None) == 409 or str(exc).startswith("Resend HTTP 409")


def is_uncertain(row: InvoiceReminderEmail) -> bool:
    """L'ultimo tentativo della riga potrebbe essere arrivato al cliente? Sì
    per una riga interrotta a metà invio e per ogni fallimento che non sia
    «certamente non partita» (PDF non generato, rifiuto di Resend)."""
    if row.status == STATUS_SENDING:
        return True
    return row.status == STATUS_FAILED and (row.error_kind or ERROR_OTHER) not in AUTO_RETRY_KINDS


def _utc_now(now_utc: Optional[datetime] = None) -> datetime:
    """L'istante dato (o adesso) in UTC senza fuso, come le date del registro."""
    now = now_utc or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo is not None else now


def needs_confirmation(row: InvoiceReminderEmail, now: Optional[datetime] = None) -> bool:
    """«Riprova» su questa riga può far arrivare al cliente un DOPPIONE che
    Resend non fermerebbe? Sì per un invio REALE dall'esito incerto dopo un
    409 (gli era già arrivato) o nato da più di un giorno (Resend ha
    dimenticato la chiave). Serve la conferma esplicita di chi ha controllato
    su Resend che l'email NON è stata consegnata. Una riga di prova o di test
    non chiede nulla: il cliente non aveva ricevuto niente."""
    if not is_uncertain(row) or row.mode != MODE_REALE:
        return False
    if row.error_kind == ERROR_CONFLICT:
        return True
    born = row.created_at or row.attempted_at
    if born is None:
        return True  # età ignota: non si può contare su Resend
    return (now or _utc_now()) - born > RESEND_KEY_MEMORY


def _carried_doubt(existing: Optional[InvoiceReminderEmail], mode: str) -> Optional[str]:
    """Il tipo di esito incerto che un nuovo tentativo REALE eredita da quello
    precedente (None se non c'è nulla da ereditare): la riga era un invio
    reale che potrebbe essere arrivato."""
    if existing is None or mode != MODE_REALE or existing.mode != MODE_REALE or not is_uncertain(existing):
        return None
    return (existing.error_kind or ERROR_OTHER) if existing.status == STATUS_FAILED else ERROR_OTHER


def _kind_after(carried: Optional[str], kind: str) -> str:
    """Il tipo di errore da scrivere dopo un tentativo fallito: un 409 visto
    una volta resta; un dubbio non diventa mai un rifiuto «certo»."""
    if carried == ERROR_CONFLICT:
        return ERROR_CONFLICT
    if carried and kind in AUTO_RETRY_KINDS:
        return carried
    return kind


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


def _round_invoices(session: Session, dates: List[date]) -> List[Invoice]:
    """Le fatture che il giro guarda: quelle con una scadenza del giro, più
    quelle che scadono il giorno dopo (si possono unire all'email di oggi)."""
    near = near_due_date(dates)
    out: List[Invoice] = []
    for due in list(dates) + ([near] if near is not None and near not in dates else []):
        out.extend(candidate_invoices(session, due))
    return out


def preview(session: Session, due_date: Optional[date] = None, now_utc: Optional[datetime] = None,
            mode: Optional[str] = None, due_dates: Optional[List[date]] = None, cfg=None) -> List[dict]:
    """Cosa farebbe il giro per le scadenze date: NESSUNA scrittura. Una voce
    per fattura; le fatture della stessa email portano lo stesso testo e
    l'elenco delle altre (`group_invoices`)."""
    cfg = cfg or config
    dates = due_dates or ([due_date] if due_date else target_due_dates(now_utc))
    mode = mode or resolve_mode(cfg)[0]
    today = italian_today(now_utc)
    out = []
    for customer_id, invoices in by_customer(_round_invoices(session, dates)):
        out.extend(_preview_customer(session, customer_id, invoices, dates, mode, today, cfg))
    return out


def _preview_customer(session: Session, customer_id: int, invoices: List[Invoice], dates: List[date], mode: str,
                      today: date, cfg) -> List[dict]:
    plan = plan_customer(session, invoices, dates, mode, customer_id=customer_id)
    in_email = {inv.id for inv in plan.email}
    group_msg = None
    if plan.email:
        overdue = overdue_invoices(session, plan.customer.id, exclude_ids=[i.id for i in plan.email])
        group_msg = compose_many(plan.customer, plan.email, overdue, today)
    out = []
    for inv in in_email_order(invoices):
        early = inv.due_date not in dates
        if early and inv.id not in in_email:
            continue  # scade il giorno dopo e oggi non parte con nessuna: aspetta il suo giorno
        customer = inv.customer
        existing = plan.existing.get(inv.id) or _existing_row(session, inv)
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
            "due_date": inv.due_date.isoformat(),
            "due_label": due_label(inv.due_date, today),
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
            # Una email per cliente: quante fatture porta l'email di questa
            # fattura, quali sono le altre, e se questa è unita in anticipo
            # (scade il giorno dopo le scadenze del giro).
            "group_size": 1,
            "group_invoices": [],
            "merged_early": False,
        }
        if inv.id in in_email:
            entry.update({
                "overdue_count": group_msg["overdue_count"],
                "overdue_total": group_msg["overdue_total"],
                "subject": group_msg["subject"],
                "text": group_msg["text"],
                "group_size": len(plan.email),
                "group_invoices": [other.invoice_number for other in plan.email if other.id != inv.id],
                "merged_early": inv.id in plan.early_ids,
            })
        elif not reason:
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


def _reserve_all(session: Session, rows: List[InvoiceReminderEmail]) -> bool:
    """Riserva INSIEME le righe di un'email (status `sending`) prima
    dell'invio. False se un altro giro ne aveva già riservata una: in quel
    caso l'email non parte."""
    if len(rows) == 1:
        return _reserve(session, rows[0])
    attempted_at = datetime.utcnow()
    for row in rows:
        row.status = STATUS_SENDING
        row.attempted_at = attempted_at
        session.add(row)
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False


def _carried_doubt_many(rows: Iterable[Optional[InvoiceReminderEmail]], mode: str) -> Optional[str]:
    """Il dubbio che un nuovo tentativo REALE eredita dalle righe dell'email:
    un 409 visto su una di loro vale per tutte; altrimenti il primo esito
    incerto; None se nessuna riga era un invio reale che potrebbe essere
    arrivato."""
    carried = [kind for kind in (_carried_doubt(row, mode) for row in rows) if kind]
    if not carried:
        return None
    return ERROR_CONFLICT if ERROR_CONFLICT in carried else carried[0]


def _record_skip(session: Session, inv: Invoice, row: Optional[InvoiceReminderEmail], reason: str, mode: str,
                 summary: dict) -> Optional[InvoiceReminderEmail]:
    """Registra una fattura del giro esclusa, col motivo (mai scartata in
    silenzio). None se un altro giro aveva già scritto la sua riga."""
    customer = inv.customer
    row = row or InvoiceReminderEmail(invoice_id=inv.id, due_date=inv.due_date)
    row.customer_id = customer.id if customer else None
    row.mode = mode
    row.amount_due = float(inv.amount_due or 0)
    row.error = None
    row.error_kind = None
    row.status = STATUS_SKIPPED
    row.skip_reason = reason
    row.group_key = None
    row.idempotency_key = None
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


def _process_group(session: Session, customer: Customer, invoices: List[Invoice], *, key: str, mode: str, mailer,
                   cfg, summary: dict, existing: Dict[int, InvoiceReminderEmail], today: Optional[date],
                   idempotency_key: str, carried: Optional[str] = None,
                   release_key: Optional[str] = None) -> None:
    """Compone, registra e (fuori dalla prova) invia UN promemoria per le
    fatture date dello stesso cliente. Una riga per fattura, riservate
    insieme PRIMA dell'invio e chiuse insieme dopo: le fatture di un'email
    hanno sempre lo stesso esito. Con una fattura sola è l'email di sempre.

    `idempotency_key`: la chiave verso Resend di questo tentativo.
    `carried`: il dubbio ereditato da un tentativo precedente (stessa
    modalità REALE). Un esito incerto resta incerto finché l'email non parte
    davvero: un nuovo fallimento «certo» (PDF, rifiuto) NON trasforma le
    righe in qualcosa che il giro ritenterebbe da solo; e dopo un 409 resta
    «conflict» qualunque cosa succeda poi. In prova e in test il cliente non
    riceve nulla, e lì tutto resta com'era.
    `release_key`: la chiave di un tentativo incerto RIUSATA da questo
    ritento e ancora ricordata da Resend. Se l'invio riesce, Resend non
    l'aveva mai vista: le righe rimaste fuori dal ritento che la portano
    ancora la perdono (vedi _release_siblings)."""
    invoices = in_email_order(invoices)
    strangers = [inv.invoice_number for inv in invoices if inv.customer_id != customer.id]
    if strangers:
        # Cintura: nessuna strada deve poter mettere nell'email di un cliente
        # la fattura di un altro.
        raise ValueError(f"fatture di un altro cliente nell'email: {', '.join(map(str, strangers))}")
    overdue = overdue_invoices(session, customer.id, exclude_ids=[inv.id for inv in invoices])
    msg = compose_many(customer, invoices, overdue, today or italian_today())
    real_to = normalize_email(customer.email)
    test_to = (getattr(cfg, "REMINDER_EMAIL_TEST_TO", "") or "").strip()
    to_email = test_to if mode == MODE_TEST else real_to
    # Copie (commerciale + fatture@) solo nell'invio REALE: in test tutto va
    # al solo indirizzo di prova.
    cc = carbon_copies(customer, cfg) if mode == MODE_REALE else []
    many = len(invoices) > 1
    label = ", ".join(str(inv.invoice_number) for inv in invoices)
    rows: List[InvoiceReminderEmail] = []
    for inv in invoices:
        row = existing.get(inv.id) or InvoiceReminderEmail(invoice_id=inv.id, due_date=inv.due_date)
        row.customer_id = customer.id
        row.mode = mode
        row.amount_due = float(inv.amount_due or 0)
        row.error = None
        row.error_kind = None
        row.skip_reason = None
        row.subject = msg["subject"]
        row.body_text = msg["text"]
        row.overdue_count = msg["overdue_count"]
        row.overdue_total = msg["overdue_total"]
        row.original_to = real_to
        row.to_email = to_email
        row.cc_json = cc
        row.group_key = key if many else None
        row.idempotency_key = idempotency_key
        rows.append(row)

    # Riserva PRIMA di fare qualsiasi cosa costosa o irreversibile.
    if not _reserve_all(session, rows):
        summary["already"] += len(rows)
        return

    def close(outcome: str) -> None:
        for row in rows:
            session.add(row)
        session.commit()
        # Solo a scrittura riuscita l'esito entra nei conteggi.
        summary[outcome] += 1
        if outcome in ("sent", "prepared"):
            summary["invoices"] = summary.get("invoices", 0) + len(rows)
        for row, inv in zip(rows, invoices):
            summary["items"].append(_item(row, inv, customer))

    try:
        attachments = build_group_attachments(customer, invoices, overdue)
    except Exception as e:  # il PDF non deve far cadere il giro
        logger.error(f"Promemoria {label}: PDF non generato: {e}", exc_info=True)
        for row in rows:
            row.status = STATUS_FAILED
            row.error_kind = _kind_after(carried, ERROR_PDF)
            row.error = f"PDF non generato: {e}"[:500]
        close("failed")
        return
    names = [name for name, _ in attachments]
    for row in rows:
        row.attachments_json = names

    if mode == MODE_PROVA:
        for row in rows:
            row.status = STATUS_PREPARED
        close("prepared")
        return
    try:
        message_id = mailer.send(
            sender=cfg.REMINDER_EMAIL_FROM,
            to=[to_email],
            subject=msg["subject"],
            text=msg["text"],
            html=msg["html"],
            reply_to=(getattr(cfg, "REMINDER_EMAIL_REPLY_TO", "") or None),
            cc=cc or None,
            bcc=(getattr(cfg, "REMINDER_EMAIL_BCC", "") or None) if mode == MODE_REALE else None,
            attachments=attachments,
            idempotency_key=idempotency_key,
        )
    except Exception as e:  # messaggio già privo di segreti (MailSendError)
        logger.error(f"Promemoria {label}: invio fallito: {e}")
        if is_conflict(e) and mode == MODE_REALE:
            kind, error = ERROR_CONFLICT, (str(e)[:500 - len(CONFLICT_NOTE)] + CONFLICT_NOTE)
        else:
            kind, error = _kind_after(carried, _classify_error(e)), str(e)[:500]
        for row in rows:
            row.status = STATUS_FAILED
            row.error_kind = kind
            row.error = error
        close("failed")
        return
    sent_at = datetime.utcnow()
    for row in rows:
        row.status = STATUS_SENT
        row.sent_at = sent_at
        row.provider_message_id = message_id or None
    if release_key:
        _release_siblings(session, release_key, [row.id for row in rows])
    close("sent")


def _release_siblings(session: Session, key: str, sent_ids: List[int]) -> None:
    """Un ritento ha RIUSATO la chiave `key`, ancora ricordata da Resend, ed
    è stato accettato: Resend non l'aveva mai vista, quindi nessun tentativo
    con quella chiave gli era arrivato. Le altre righe che la portano ancora
    (fatture della stessa email rimaste fuori da questo ritento) non devono
    più usarla, o al loro ritento Resend risponderebbe 409 per l'email
    appena partita — che di loro non parla. Restano ferme (decide un
    operatore), ma senza quella chiave."""
    siblings = (
        session.query(InvoiceReminderEmail)
        .filter(InvoiceReminderEmail.idempotency_key == key, InvoiceReminderEmail.id.notin_(sent_ids),
                InvoiceReminderEmail.status.in_((STATUS_FAILED, STATUS_SENDING)))
        .all()
    )
    for sibling in siblings:
        sibling.idempotency_key = None
        session.add(sibling)


def _mark_unexpected(session: Session, facts: List[tuple], mode: str, carried: Optional[str], error: Exception,
                     summary: dict, customer: Optional[Customer]) -> None:
    """Dopo un errore imprevisto: rollback, e ogni fattura dell'email ha una
    riga `failed` (ferma finché un operatore non la riprende: l'invio
    potrebbe essere partito). Una riga già scritta come inviata da questo
    tentativo non si tocca: l'errore è arrivato dopo."""
    try:
        session.rollback()
        marked = []
        for invoice_id, due, customer_id, amount_due, inv in facts:
            row = session.query(InvoiceReminderEmail).filter_by(invoice_id=invoice_id, due_date=due).first()
            if row is None:
                row = InvoiceReminderEmail(invoice_id=invoice_id, due_date=due, customer_id=customer_id,
                                           mode=mode, amount_due=amount_due)
            elif row.status == STATUS_SENT and row.mode == mode:
                continue
            row.status = STATUS_FAILED
            row.error_kind = _kind_after(carried, ERROR_OTHER)
            row.error = f"Errore imprevisto: {error}"[:500]
            session.add(row)
            marked.append((row, inv))
        if marked:
            session.commit()
            summary["failed"] += 1
            for row, inv in marked:
                summary["items"].append(_item(row, inv, customer))
    except Exception as inner:
        logger.error(f"Promemoria: impossibile registrare l'errore: {inner}")
        try:
            session.rollback()
        except Exception:
            pass


def _facts(invoices: Iterable[Invoice]) -> List[tuple]:
    return [(inv.id, inv.due_date, inv.customer_id, float(inv.amount_due or 0), inv) for inv in invoices]


def _process_group_safely(session: Session, customer: Customer, invoices: List[Invoice], **kwargs) -> None:
    """Un'email non deve mai far saltare le altre: eccezioni impreviste →
    rollback, righe `failed` (other) se possibile, e si prosegue."""
    facts = _facts(invoices)
    try:
        _process_group(session, customer, invoices, **kwargs)
    except Exception as e:
        logger.error(f"Promemoria {', '.join(str(f[4].invoice_number) for f in facts)}: errore imprevisto: {e}",
                     exc_info=True)
        _mark_unexpected(session, facts, kwargs["mode"], kwargs.get("carried"), e, kwargs["summary"], customer)


def _record_skip_safely(session: Session, inv: Invoice, row: Optional[InvoiceReminderEmail], reason: str,
                        mode: str, summary: dict) -> None:
    """Come sopra per una fattura esclusa. Qui non c'è mai un dubbio da
    custodire: si registra «saltata» solo ciò che certamente non era partito
    (le righe dall'esito incerto non arrivano fin qui, né dal giro né da
    «Riprova»)."""
    facts = _facts([inv])
    try:
        _record_skip(session, inv, row, reason, mode, summary)
    except Exception as e:
        logger.error(f"Promemoria {facts[0][4].invoice_number}: errore imprevisto: {e}", exc_info=True)
        _mark_unexpected(session, facts, mode, None, e, summary, inv.customer)


def _run_customer(session: Session, customer_id: int, invoices: List[Invoice], dates: List[date], *, mode: str,
                  mailer, cfg, summary: dict, today: date) -> None:
    """Il giro per UN cliente: al massimo una email, con tutte le sue fatture
    pronte. Prima l'email (nessuna scrittura tra la lettura dello stato e la
    riserva), poi le esclusioni."""
    plan = plan_customer(session, invoices, dates, mode, customer_id=customer_id)
    summary["candidates"] += plan.candidates
    summary["already"] += len(plan.already)
    skipped = [(inv, plan.existing.get(inv.id), reason) for inv, reason in plan.skipped]
    if plan.email:
        summary["merged_early"] = summary.get("merged_early", 0) + len(plan.early_ids)
        key = group_key(plan.customer.id, plan.email, today)
        # Nessun dubbio ereditato: il giro rilavora solo righe che certamente
        # non erano partite (needs_processing lascia ferme le incerte).
        _process_group_safely(
            session, plan.customer, plan.email, key=key, mode=mode, mailer=mailer, cfg=cfg, summary=summary,
            existing=plan.existing, today=today, idempotency_key=attempt_key(plan.email, mode, key),
        )
    for inv, row, reason in skipped:
        _record_skip_safely(session, inv, row, reason, mode, summary)


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
        # Una email per cliente: `sent`/`prepared`/`failed` contano le EMAIL,
        # `invoices` le fatture dentro quelle inviate o preparate,
        # `merged_early` quelle unite perché scadono il giorno dopo.
        summary.update({"invoices": 0, "merged_early": 0, "broken": 0})
        today = italian_today(now_utc)
        try:
            for customer_id, invoices in by_customer(_round_invoices(session, dates)):
                try:
                    _run_customer(session, customer_id, invoices, dates, mode=mode, mailer=mailer, cfg=cfg,
                                  summary=summary, today=today)
                except Exception as e:  # un cliente non ferma mai gli altri
                    logger.error(f"Promemoria cliente {customer_id}: giro interrotto per questo cliente: {e}",
                                 exc_info=True)
                    summary["broken"] += 1
                    try:
                        session.rollback()
                    except Exception:
                        pass
            # Nessun «riporto» di righe lavorate in prova/test fuori dalle date
            # del giro: il promemoria parte il giorno prima (mai il giorno
            # stesso) e mai nel weekend, quindi al passaggio in reale le date
            # del giro sono le uniche da coprire — le righe prova/test di
            # quelle date vengono riprese qui sopra (needs_processing). Una
            # scadenza il cui «giorno prima» è passato in prova/test resta
            # senza email: il registro lo mostra (riga prova/test).
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        _reminder_lock.release()

    logger.info(
        "Promemoria email (scadenze %s, modalità %s): candidate=%d email_inviate=%d email_preparate=%d "
        "fatture_nelle_email=%d di_cui_unite=%d saltate=%d email_fallite=%d non_lavorate=%d",
        ", ".join(summary["due_dates"]) or "nessuna (weekend)", mode, summary["candidates"], summary["sent"],
        summary["prepared"], summary["invoices"], summary["merged_early"], summary["skipped"], summary["failed"],
        summary["already"],
    )
    return summary


def email_group_of(session: Session, invoice: Invoice, *, now_utc: Optional[datetime] = None,
                   cfg=None) -> List[Invoice]:
    """Le fatture dell'email che il cliente riceverebbe OGGI per la fattura
    data: se il giro di oggi la manderebbe insieme ad altre fatture dello
    stesso cliente, tutte; altrimenti lei sola. L'email del cliente non
    conta (serve all'invio di prova, che va a un indirizzo interno)."""
    cfg = cfg or config
    dates = target_due_dates(now_utc)
    if not dates or invoice.customer_id is None:
        return [invoice]
    mine = [inv for inv in _round_invoices(session, dates) if inv.customer_id == invoice.customer_id]
    plan = plan_customer(session, mine, dates, resolve_mode(cfg)[0], customer_id=invoice.customer_id,
                         ignore_email=True)
    if any(inv.id == invoice.id for inv in plan.email):
        return list(plan.email)
    return [invoice]


def send_test(session: Session, invoice_id: int, to_address: str, *, mailer=None, cfg=None) -> dict:
    """Invia a UN indirizzo scelto l'email che il cliente riceverebbe per la
    fattura data — con le altre sue fatture, se oggi partirebbero insieme
    (oggetto con prefisso [PROVA], nessuna copia, registro intoccato). Serve
    a far leggere testo e allegati veri prima di accendere l'invio ai
    clienti. Richiede Resend configurato."""
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
    group = email_group_of(session, inv, cfg=cfg)
    overdue = overdue_invoices(session, customer.id, exclude_ids=[i.id for i in group])
    msg = compose_many(customer, group, overdue, italian_today())
    attachments = build_group_attachments(customer, group, overdue)
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
        "invoices": [i.invoice_number for i in group],
        "ragione_sociale": customer.ragione_sociale, "subject": f"[PROVA] {msg['subject']}",
        "attachments": [name for name, _ in attachments], "provider_message_id": message_id or None,
        "would_cc": carbon_copies(customer, cfg),
    }


def _retry_members(session: Session, row: InvoiceReminderEmail, inv: Invoice, today: date, *, mode: str,
                   force: bool, now: datetime) -> List[Tuple[Invoice, InvoiceReminderEmail]]:
    """Le ALTRE righe della stessa email ancora ferme (fallite o interrotte)
    che si possono ritentare insieme alla riga scelta, così il cliente
    riceve di nuovo UNA email e non una per fattura. Resta fuori — e la sua
    riga non cambia — una fattura nel frattempo saldata, con la scadenza
    cambiata o passata, passata a un ALTRO cliente, con un motivo di
    esclusione, oppure il cui esito incerto non si può riprendere adesso
    (fuori dal reale, o senza la conferma «ho controllato su Resend» quando
    serve)."""
    if not row.group_key:
        return []
    others = (
        session.query(InvoiceReminderEmail)
        .filter(InvoiceReminderEmail.group_key == row.group_key, InvoiceReminderEmail.id != row.id)
        .order_by(InvoiceReminderEmail.id.asc())
        .all()
    )
    out = []
    for other in others:
        if other.status not in (STATUS_FAILED, STATUS_SENDING):
            continue
        member = session.query(Invoice).filter(Invoice.id == other.invoice_id).first()
        if member is None or member.status in NOT_REMINDABLE_STATUSES or float(member.amount_due or 0) <= 0:
            continue
        if member.customer_id != inv.customer_id:
            continue
        if member.due_date is None or member.due_date != other.due_date or other.due_date < today:
            continue
        if skip_reason(member, member.customer):
            continue
        if is_uncertain(other) and other.mode == MODE_REALE:
            if mode != MODE_REALE:
                continue
            if needs_confirmation(other, now) and not force:
                continue
        out.append((member, other))
    return out


def retry_row(session: Session, row: InvoiceReminderEmail, *, mailer=None, cfg=None, mode: Optional[str] = None,
              expected_mode: Optional[str] = None, force: bool = False,
              now_utc: Optional[datetime] = None) -> dict:
    """Ritenta UNA riga ferma (`failed` o `sending` orfana) di una scadenza
    non ancora passata, insieme alle altre fatture della stessa email ancora
    ferme: il giro quotidiano guarda solo le prossime scadenze. La fattura
    deve essere ancora da ricordare, con la stessa scadenza.

    Un invio dall'esito incerto (potrebbe essere arrivato) resta tale finché
    l'email non parte davvero: se oggi non si può inviare (email mancante,
    forse pagata…) la riga resta com'è e l'operatore legge il perché — non
    diventa «saltata». Il ritento continua quel tentativo: stessa chiave
    verso Resend, qualunque siano oggi le fatture, così per un giorno è
    Resend a fermare il doppione (409). `force`: l'operatore conferma di
    aver controllato su Resend che l'email NON è stata consegnata; serve
    dopo un 409 e per un invio incerto più vecchio di un giorno
    (needs_confirmation). Dopo un 409 la conferma fa partire un tentativo
    con una chiave nuova."""
    cfg = cfg or config
    if row.status not in (STATUS_FAILED, STATUS_SENDING):
        raise ValueError("si ritentano solo i promemoria falliti o interrotti")
    today = italian_today()
    if row.due_date and row.due_date < today:
        raise ValueError("scadenza già passata: non è più un promemoria (la fattura è ora tra le scadute)")
    inv = session.query(Invoice).filter(Invoice.id == row.invoice_id).first()
    if inv is None:
        raise ValueError("fattura non più presente")
    if inv.status in NOT_REMINDABLE_STATUSES or float(inv.amount_due or 0) <= 0:
        raise ValueError("fattura non più da ricordare (pagata, annullata o contestata)")
    if inv.due_date != row.due_date:
        raise ValueError("la scadenza della fattura è cambiata: questo promemoria non è più valido")
    if not _reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, missing, mailer, own_mailer = _resolve_runtime(cfg, mailer, mode)
        try:
            if expected_mode and expected_mode != mode:
                raise ModeMismatch(mode, expected_mode)
            now = _utc_now(now_utc)
            summary = _empty_summary(row.due_date, mode, missing)
            summary.update({"invoices": 0, "merged_early": 0, "broken": 0})
            reason = skip_reason(inv, inv.customer)
            if is_uncertain(row) and row.mode == MODE_REALE:
                # Un invio REALE incerto non si «ritenta» in prova o in test:
                # la riga cambierebbe modalità e perderebbe il suo dubbio, e
                # il prossimo ritento reale partirebbe senza nessuna cautela.
                if mode != MODE_REALE:
                    raise ValueError(f"{REAL_DOUBT_ONLY_IN_REAL} (ora: {MODE_LABELS.get(mode, mode)})")
                if reason:
                    raise ValueError(f"promemoria non inviabile ora: {SKIP_LABELS.get(reason, reason)}")
                if needs_confirmation(row, now) and not force:
                    raise ValueError(CONFIRMATION_NEEDED)
            if reason:
                # Solo per ciò che CERTAMENTE non era partito (o fuori dal
                # reale): come nel giro, la riga dice perché oggi non parte.
                summary["candidates"] = 1
                _record_skip_safely(session, inv, row, reason, mode, summary)
                return summary
            members = [(inv, row)] + _retry_members(session, row, inv, today, mode=mode, force=force, now=now)
            ready = [member for member, _row in members]
            rows = [member_row for _member, member_row in members]
            summary["candidates"] = len(members)
            key = group_key(inv.customer_id, ready, today)
            idempotency_key = attempt_key(ready, mode, key)
            release_key = None
            # Righe REALI dall'esito incerto: il ritento continua quel tentativo.
            uncertain = [r for r in rows if is_uncertain(r) and r.mode == MODE_REALE]
            if uncertain:
                if force and any(r.error_kind == ERROR_CONFLICT for r in uncertain):
                    # Dopo un 409 la chiave di quel tentativo è bruciata
                    # (Resend risponderebbe ancora 409): serve una chiave nuova.
                    idempotency_key += f"-f{now.strftime('%Y%m%d%H%M%S%f')}"
                else:
                    # La chiave del tentativo incerto, qualunque siano oggi le
                    # fatture: se gli era arrivato, Resend risponde 409 e non
                    # rimanda. Vale anche quando si conferma solo per l'età:
                    # se Resend la ricorda ancora, ferma lui il doppione. Una
                    # riga nata prima della colonna ha la chiave di sempre.
                    stored = next((r.idempotency_key for r in uncertain if r.idempotency_key), None)
                    if stored:
                        idempotency_key = stored
                        born = [b for b in ((r.created_at or r.attempted_at) for r in uncertain) if b is not None]
                        if born and now - min(born) < RESEND_KEY_MEMORY:
                            release_key = stored
            _process_group_safely(
                session, inv.customer, ready, key=key, mode=mode, mailer=mailer, cfg=cfg, summary=summary,
                existing={member.id: member_row for member, member_row in members}, today=today,
                idempotency_key=idempotency_key, carried=_carried_doubt_many(rows, mode), release_key=release_key,
            )
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        _reminder_lock.release()
    return summary
