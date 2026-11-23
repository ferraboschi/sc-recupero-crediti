"""Secondo avviso email: fatture scadute da N giorni e ancora non saldate.

Richiesta owner (24/11/2026): dopo il promemoria del giorno prima della
scadenza, se a una settimana dalla scadenza la fattura risulta ancora non
pagata il cliente riceve «un'ulteriore email», più diretta: abbiamo rilevato
che la fattura è scaduta da 7 giorni e chiediamo il saldo. E ancora
(24/11/2026): «Non si devono ricevere due mail. In caso di scadenze prossime
unire le mail e adeguare il testo. Scadenza prossima significa ± un giorno».

Regole (ognuna ha il suo test in tests/test_followup_email.py):

- QUANDO. Il giro quotidiano (subito dopo quello dei promemoria, quindi dopo
  il sync delle fatture) guarda le scadenze tra oggi−(N+recupero) e oggi−N
  (default N = 7, recupero = 6): una fattura entra il PRIMO giorno
  lavorativo in cui è scaduta da almeno N giorni. Mai nel weekend: una
  scadenza di sabato o domenica parte il lunedì. Il testo dice sempre i
  giorni VERI di ritardo al momento dell'invio. I giorni di recupero servono
  a non perdere una fattura per un giro saltato (riavvio, sync fermo).
- UNA EMAIL PER CLIENTE. Tutte le fatture di un cliente che il giro di oggi
  deve sollecitare partono in UN'UNICA email (elenco delle fatture, totale,
  una copia di cortesia per ciascuna). Nella stessa email entrano anche le
  sue fatture con la scadenza del giorno dopo (NEAR_DUE_DAYS = 1: scadute da
  N−1 giorni): così due scadenze a un giorno di distanza non producono due
  email in due giorni. Da sola, una fattura scaduta da N−1 giorni non fa
  partire nulla: aspetta il suo giorno.
- A CHI. Solo alle fatture il cui promemoria pre-scadenza è stato inviato
  davvero al cliente (riga `sent` in modalità reale per la stessa scadenza):
  è un secondo avviso, mai il primo. Le altre sono FUORI PERIMETRO: il giro
  non scrive nulla per loro (niente righe «saltata» nello storico del
  cliente), l'anteprima le elenca col motivo («promemoria non inviato»).
- IL CONTROLLO DEL PAGAMENTO. L'email afferma «non risulta saldata»: parte
  solo se l'ultimo sync delle fatture è RIUSCITO, completo e recente
  (REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS). Altrimenti il giro si ferma senza
  scrivere nulla e la finestra di recupero lo riprende il giorno dopo.
  Restano valide tutte le esclusioni del promemoria (cliente escluso/unito,
  avvocato, assegno in incasso, scadenza stimata, forse già pagata,
  abbinamento da verificare, email assente o non valida) più: pratica in
  attesa decisa dall'operatore, pagamento promesso segnato dall'operatore.
  Lo stato delle fatture di un cliente si rilegge subito prima della sua
  email. Una fattura esclusa resta fuori dall'email; le altre partono.
- LE ALTRE SCADUTE. L'email cita (testo, totale, riepilogo) solo le altre
  scadute CERTE del cliente: fuori quelle forse già pagate, con scadenza
  stimata, con pagamento promesso, consegnate all'avvocato o con assegno in
  incasso. Meglio un totale più basso che una richiesta sbagliata.
- UNA SOLA VOLTA. Una riga per (fattura, scadenza) in
  `invoice_followup_emails`; le righe della stessa email condividono
  `group_key` e vengono RISERVATE insieme (status sending) prima dell'invio.
  Stesse protezioni del promemoria (reminder_email.py): Idempotency-Key
  verso Resend, ritento automatico solo dei fallimenti certamente senza
  invio, un solo giro alla volta (lock condiviso).
- SPENTO DI DEFAULT. Senza REMINDER_FOLLOWUP_ENABLED=true la modalità è
  «off»: nessuna riga scritta, nessuna email; il pannello mostra l'anteprima
  di ciò che partirebbe. Da acceso segue la modalità del promemoria
  (prova / test / reale).

Non tocca il recupero manuale: non crea azioni, non cambia lo stadio delle
fatture né lo stato del cliente (i solleciti di Luigi restano i suoi).

Il motore del promemoria (reminder_email.py) non è modificato: qui si usano
le sue regole comuni (esclusioni, modalità, copie, lock) e si ripete, per
un'email con più fatture, la sua sequenza di invio.
"""
import hashlib
import html
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import config
from backend.database import (
    Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail, RecoveryCase, SyncState,
)
from backend.engine import reminder_email as base
from backend.engine.action_invoices import delivered_invoice_ids
from backend.engine.format import fmt_date, fmt_eur, italian_today, safe_filename
from backend.engine.reminder_email import (
    ERROR_OTHER, ERROR_PDF, MODE_LABELS, MODE_PROVA, MODE_REALE, MODE_TEST, NOT_REMINDABLE_STATUSES,
    SKIP_LABELS, STATUS_FAILED, STATUS_PREPARED, STATUS_SENDING, STATUS_SENT, STATUS_SKIPPED, ModeMismatch,
    NotFound, ReminderBusy, carbon_copies, invoice_label, needs_processing, normalize_email,
    overdue_invoices, resolve_mode, skip_reason,
)

logger = logging.getLogger(__name__)

KIND_KEY = "followup"          # il «tipo» delle righe nel registro unito delle email automatiche
KIND_LABEL = "Secondo avviso"  # per i log

MODE_OFF = "off"
FOLLOWUP_MODE_LABELS = dict(MODE_LABELS, **{MODE_OFF: "Spento (solo anteprima)"})

# «Scadenza prossima significa ± un giorno» (owner, 24/11/2026): nell'email di
# oggi entrano anche le fatture dello stesso cliente che scadono il giorno
# dopo l'ultima scadenza del giro.
NEAR_DUE_DAYS = 1

# Motivi che riguardano solo l'indirizzo del cliente: un invio di prova (che
# va a un indirizzo interno) non ne ha bisogno.
EMAIL_SKIPS = ("senza_email", "email_non_valida")

# Oltre questo numero di fatture la causale non le elenca più una per una
# (un bonifico SEPA ha 140 caratteri di causale).
MAX_NUMBERS_IN_CAUSALE = 3

BLOCK_LABELS = {
    "off": "Secondo avviso spento (REMINDER_FOLLOWUP_ENABLED)",
    "weekend": "Weekend: nessun invio",
    "sync": "Sync fatture non aggiornato: invio sospeso",
}


# ── Configurazione e calendario ───────────────────────────────────────

def _int_setting(cfg, name: str, default: int, minimum: int, maximum: int) -> int:
    """Intero di configurazione, sempre dentro [minimum, maximum]; un valore
    assente o non numerico vale il default (una variabile scritta male su
    Render non deve far cadere il giro né, peggio, anticipare gli invii)."""
    try:
        value = int(str(getattr(cfg, name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def followup_days(cfg=None) -> int:
    """Giorni dalla scadenza dopo i quali parte il secondo avviso (1-365)."""
    return _int_setting(cfg or config, "REMINDER_FOLLOWUP_DAYS", 7, 1, 365)


def followup_lookback(cfg=None) -> int:
    """Giorni di recupero oltre il primo giorno utile (2-60; min 2 = weekend)."""
    return _int_setting(cfg or config, "REMINDER_FOLLOWUP_LOOKBACK_DAYS", 6, 2, 60)


def max_sync_age_hours(cfg=None) -> int:
    return _int_setting(cfg or config, "REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS", 3, 1, 48)


def resolve_followup_mode(cfg=None) -> Tuple[str, List[str]]:
    """Modalità effettiva del secondo avviso + variabili mancanti. Spento
    finché REMINDER_FOLLOWUP_ENABLED non è vero; da acceso è la modalità del
    promemoria (stessa configurazione Resend, stesso indirizzo di prova)."""
    cfg = cfg or config
    mode, missing = resolve_mode(cfg)
    if not getattr(cfg, "REMINDER_FOLLOWUP_ENABLED", False):
        return MODE_OFF, ["REMINDER_FOLLOWUP_ENABLED"] + list(missing)
    return mode, list(missing)


def followup_window(now_utc: Optional[datetime] = None, cfg=None) -> Optional[Tuple[date, date]]:
    """Le scadenze (prima, ultima) che FANNO PARTIRE un'email nel giro di
    oggi; None nel weekend. L'ultima è oggi−N: una fattura entra il giorno in
    cui è scaduta da N giorni e resta candidata per i giorni di recupero."""
    today = italian_today(now_utc)
    if today.weekday() >= 5:
        return None
    days = followup_days(cfg)
    return today - timedelta(days=days + followup_lookback(cfg)), today - timedelta(days=days)


def merge_until(window: Tuple[date, date]) -> date:
    """L'ultima scadenza che può essere UNITA all'email di oggi: il giorno
    dopo l'ultima scadenza della finestra (scadenza prossima = ± un giorno)."""
    return window[1] + timedelta(days=NEAR_DUE_DAYS)


def sync_gate(session: Session, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Il dato «non pagata» è affidabile? Sì solo se l'ULTIMO sync delle
    fatture è riuscito, completo e recente. Nessun dettaglio dell'errore del
    sync viene riportato qui (lo mostra già «Salute sistema»)."""
    max_hours = max_sync_age_hours(cfg)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    out = {"ok": False, "max_age_hours": max_hours, "last_sync": None, "age_minutes": None, "detail": None}
    row = session.query(SyncState).filter(SyncState.key == "invoices").first()
    if row is None or row.last_sync is None:
        out["detail"] = "nessun sync delle fatture registrato"
        return out
    age_minutes = int((now - row.last_sync).total_seconds() // 60)
    out["last_sync"] = row.last_sync.isoformat()
    out["age_minutes"] = age_minutes
    result = row.result if isinstance(row.result, dict) else {}
    fatturapro = result.get("fatturapro") if isinstance(result.get("fatturapro"), dict) else {}
    if not fatturapro.get("success"):
        out["detail"] = "l'ultimo sync delle fatture non è riuscito"
    elif fatturapro.get("partial"):
        out["detail"] = "l'ultimo sync delle fatture è parziale"
    elif age_minutes > max_hours * 60:
        out["detail"] = (
            f"l'ultimo sync delle fatture riuscito è di {age_minutes // 60} ore fa "
            f"(massimo {max_hours})"
        )
    elif age_minutes < -5:
        # Un sync «nel futuro» è un orologio sbagliato, non un dato fresco.
        out["detail"] = "l'orario dell'ultimo sync delle fatture non è plausibile"
    else:
        out["ok"] = True
    return out


# ── Selezione ─────────────────────────────────────────────────────────

def candidate_invoices(session: Session, window: Tuple[date, date],
                       customer_id: Optional[int] = None) -> List[Invoice]:
    """Fatture non pagate/annullate/contestate con scadenza nella finestra O
    nel giorno dopo (quelle che si possono unire all'email di oggi), dalla
    scadenza più vecchia. `customer_id`: solo quel cliente."""
    first = window[0]
    query = session.query(Invoice).filter(
        Invoice.due_date >= first,
        Invoice.due_date <= merge_until(window),
        Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
        Invoice.amount_due > 0,
        Invoice.customer_id.isnot(None),
    )
    if customer_id is not None:
        query = query.filter(Invoice.customer_id == customer_id)
    return query.order_by(Invoice.due_date.asc(), Invoice.customer_id.asc(), Invoice.invoice_number.asc()).all()


def by_customer(invoices: Iterable[Invoice]) -> List[List[Invoice]]:
    """Le fatture raggruppate per cliente, nell'ordine in cui il primo
    cliente compare (quindi dalla scadenza più vecchia)."""
    groups: Dict[int, List[Invoice]] = {}
    for inv in invoices:
        groups.setdefault(inv.customer_id, []).append(inv)
    return list(groups.values())


def reminder_sent(session: Session, invoice: Invoice) -> Optional[InvoiceReminderEmail]:
    """Il promemoria pre-scadenza inviato DAVVERO al cliente per questa
    fattura e questa scadenza (None se non c'è: prova, test, saltato,
    fallito, o scadenza cambiata dopo l'invio)."""
    if invoice.due_date is None:
        return None
    return (
        session.query(InvoiceReminderEmail)
        .filter(
            InvoiceReminderEmail.invoice_id == invoice.id,
            InvoiceReminderEmail.due_date == invoice.due_date,
            InvoiceReminderEmail.status == STATUS_SENT,
            InvoiceReminderEmail.mode == MODE_REALE,
        )
        .first()
    )


def followup_extra_skip(session: Session, invoice: Invoice, customer: Optional[Customer]) -> Optional[str]:
    """Esclusioni proprie del secondo avviso (dopo quelle comuni al
    promemoria). Il controllo del promemoria è ripetuto qui come cintura."""
    if reminder_sent(session, invoice) is None:
        return "senza_promemoria"
    # Attesa decisa dall'operatore (todo «Attesa» pendente sulla pratica) o
    # pagamento promesso segnato sulla fattura: un accordo preso a voce non
    # va scavalcato da un'email automatica.
    if customer is not None and (customer.recovery_status or "") == "waiting":
        return "in_attesa"
    if (invoice.status or "") == "promised":
        return "promessa"
    return None


def followup_skip_reason(session: Session, invoice: Invoice, customer: Optional[Customer],
                         ignore_email: bool = False) -> Optional[str]:
    """Perché il secondo avviso NON parte per questa fattura (None = parte):
    prima il perimetro (promemoria inviato), poi le esclusioni comuni al
    promemoria, poi quelle proprie. Vale per ogni strada: giro, anteprima,
    ritento, invio di prova. `ignore_email`: l'invio di prova va a un
    indirizzo interno, quindi l'email del cliente non serve."""
    if reminder_sent(session, invoice) is None:
        return "senza_promemoria"
    reason = skip_reason(invoice, customer)
    if ignore_email and reason in EMAIL_SKIPS:
        reason = None
    if reason is None:
        reason = followup_extra_skip(session, invoice, customer)
    return reason


def still_due(invoice: Invoice) -> bool:
    """La fattura è ancora un credito da chiedere (non pagata, annullata o
    contestata, con un residuo)? Si rilegge prima di ogni invio."""
    return invoice.status not in NOT_REMINDABLE_STATUSES and float(invoice.amount_due or 0) > 0


def other_overdue_invoices(session: Session, customer_id: int,
                           exclude_ids: Iterable[int] = ()) -> List[Invoice]:
    """Le ALTRE scadute del cliente (oltre a quelle elencate nell'email) che
    il secondo avviso può citare e sommare: solo quelle CERTE. Rispetto al
    promemoria (overdue_invoices) restano fuori le forse già pagate (sparite
    da FatturaPro, in conferma), quelle con scadenza stimata, quelle con
    pagamento promesso e quelle già consegnate all'avvocato: in un'email che
    chiede il saldo non si afferma un debito incerto né si sollecita ciò che
    segue un'altra strada."""
    excluded = set(exclude_ids or ())
    others = [
        inv for inv in overdue_invoices(session, customer_id)
        if inv.id not in excluded
        and (getattr(inv, "missing_streak", None) or 0) == 0
        and (getattr(inv, "due_date_source", None) or "assumed") == "real"
        and (inv.status or "") != "promised"
    ]
    if not others:
        return others
    case = (
        session.query(RecoveryCase)
        .filter(RecoveryCase.customer_id == customer_id, RecoveryCase.status == "open")
        .first()
    )
    if case is None:
        return others
    delivered = delivered_invoice_ids(session, case, others)
    return [inv for inv in others if inv.id not in delivered]


def _existing_row(session: Session, invoice: Invoice) -> Optional[InvoiceFollowupEmail]:
    return (
        session.query(InvoiceFollowupEmail)
        .filter_by(invoice_id=invoice.id, due_date=invoice.due_date)
        .first()
    )


def _in_email_order(invoices: Iterable[Invoice]) -> List[Invoice]:
    """Dalla scadenza più vecchia, poi per numero: l'ordine dell'elenco."""
    return sorted(invoices, key=lambda i: (i.due_date or date.max, str(i.invoice_number or ""), i.id or 0))


# ── Il piano di un cliente ────────────────────────────────────────────

@dataclass
class CustomerPlan:
    """Cosa fa il giro di oggi per le fatture di UN cliente."""
    customer: Optional[Customer] = None
    # Le fatture dell'UNICA email del cliente (vuoto = nessuna email oggi),
    # nell'ordine dell'elenco.
    email: List[Invoice] = field(default_factory=list)
    # Tra quelle, le fatture UNITE perché scadono il giorno dopo la finestra.
    early_ids: Set[int] = field(default_factory=set)
    # Righe già presenti nel registro, per fattura.
    existing: Dict[int, InvoiceFollowupEmail] = field(default_factory=dict)
    # Perché una fattura dell'email viene (ri)lavorata: nuova / ritento / …
    why: Dict[int, str] = field(default_factory=dict)
    # In finestra e in perimetro ma escluse, col motivo.
    skipped: List[Tuple[Invoice, str]] = field(default_factory=list)
    # In finestra ma senza promemoria inviato: fuori perimetro.
    out_of_scope: List[Invoice] = field(default_factory=list)
    # In finestra, con una riga che oggi non si rilavora (già inviata, ferma…).
    already: List[Tuple[Invoice, InvoiceFollowupEmail, str]] = field(default_factory=list)
    # In finestra ma saldate/annullate/contestate dopo la selezione.
    settled: List[Invoice] = field(default_factory=list)
    # Fatture in finestra e in perimetro (le «candidate» del giro).
    in_scope: int = 0


def plan_customer(session: Session, invoices: Iterable[Invoice], window: Tuple[date, date], mode: str,
                  *, ignore_email: bool = False) -> CustomerPlan:
    """Decide, per le fatture candidate di UN cliente, cosa succede oggi.

    Una fattura è PRONTA se è ancora da saldare, ha avuto il promemoria, non
    ha una riga che blocca (già inviata, fallita in modo incerto…) e non ha
    motivi di esclusione. L'email parte se almeno una pronta è NELLA finestra
    (scaduta da almeno N giorni); in quel caso porta con sé anche le pronte
    con la scadenza del giorno dopo. Le fatture del giorno dopo, da sole, non
    producono nulla oggi: né email, né righe, né conteggi.
    """
    invoices = _in_email_order(invoices)
    plan = CustomerPlan(customer=invoices[0].customer if invoices else None)
    last = window[1]
    ready: List[Tuple[Invoice, bool]] = []
    for inv in invoices:
        early = inv.due_date > last
        if not still_due(inv):
            if not early:
                plan.settled.append(inv)
            continue
        if reminder_sent(session, inv) is None:
            if not early:
                plan.out_of_scope.append(inv)
            continue
        row = _existing_row(session, inv)
        if row is not None:
            plan.existing[inv.id] = row
        if not early:
            plan.in_scope += 1
        will, why = needs_processing(row, mode)
        if not will:
            if not early:
                plan.already.append((inv, row, why))
            continue
        reason = followup_skip_reason(session, inv, inv.customer, ignore_email=ignore_email)
        if reason:
            if not early:
                plan.skipped.append((inv, reason))
            continue
        plan.why[inv.id] = why
        ready.append((inv, early))
    if any(not early for _inv, early in ready):
        plan.email = [inv for inv, _early in ready]
        plan.early_ids = {inv.id for inv, early in ready if early}
    return plan


# ── Composizione ──────────────────────────────────────────────────────

def days_overdue_on(invoice: Invoice, today: date) -> int:
    """Giorni di ritardo VERI alla data data (dalla scadenza, non dal campo
    `days_overdue` che il sync ricalcola a orari suoi)."""
    if invoice.due_date is None:
        return int(invoice.days_overdue or 0)
    return max((today - invoice.due_date).days, 0)


def _days_text(days: int) -> str:
    return "1 giorno" if days == 1 else f"{days} giorni"


def _join_it(items: List[str]) -> str:
    """«A», «A e B», «A, B e C»."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " e " + items[-1]


def compose_followup(customer: Customer, invoices: List[Invoice], overdue: List[Invoice],
                     today: Optional[date] = None) -> dict:
    """Oggetto e testo del secondo avviso per UNA email: diretto, corto, con
    tutti i dettagli — fattura (o elenco delle fatture) con importo, scadenza
    e giorni di ritardo, eventuali altre scadute col totale, coordinate di
    pagamento, cosa fare se ha già pagato. `overdue`: le ALTRE scadute certe
    del cliente, non elencate qui."""
    today = today or italian_today()
    invoices = _in_email_order(invoices)
    if not invoices:
        raise ValueError("nessuna fattura da sollecitare")
    name = (customer.ragione_sociale or "").strip() or "Cliente"
    numbers = [invoice_label(i.invoice_number) for i in invoices]
    invoices_total = round(sum(float(i.amount_due or 0) for i in invoices), 2)
    total = round(sum(float(i.amount_due or 0) for i in overdue), 2)
    grand_total = round(invoices_total + total, 2)
    lines = [
        {
            "invoice_id": inv.id,
            "number": numero,
            "amount_due": float(inv.amount_due or 0),
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
            "days_overdue": days_overdue_on(inv, today),
        }
        for inv, numero in zip(invoices, numbers)
    ]

    paragraphs = [f"Spett.le {name},"]
    if len(invoices) == 1:
        inv = invoices[0]
        importo = fmt_eur(inv.amount_due)
        scadenza = fmt_date(inv.due_date)
        subject = f"Sollecito di pagamento: fattura n. {numbers[0]} scaduta il {scadenza} ({importo})"
        paragraphs.append(
            f"abbiamo rilevato che la fattura n. {numbers[0]} di {importo} è scaduta da "
            f"{_days_text(lines[0]['days_overdue'])} (scadenza {scadenza}) e non risulta ancora saldata. "
            "Copia della fattura in allegato."
        )
    else:
        subject = f"Sollecito di pagamento: {len(invoices)} fatture scadute ({fmt_eur(invoices_total)})"
        elenco = "\n".join(
            f"- n. {line['number']} di {fmt_eur(inv.amount_due)}, scaduta da "
            f"{_days_text(line['days_overdue'])} (scadenza {fmt_date(inv.due_date)})"
            for inv, line in zip(invoices, lines)
        )
        paragraphs.append(
            "abbiamo rilevato che le seguenti fatture sono scadute e non risultano ancora saldate:\n" + elenco
        )
        # Con altre scadute il totale complessivo arriva nel paragrafo dopo:
        # qui si dice di che cosa è il totale.
        etichetta = "Totale di queste fatture" if overdue else "Totale"
        paragraphs.append(f"{etichetta}: {fmt_eur(invoices_total)}. Copia delle fatture in allegato.")
    if overdue:
        n = len(overdue)
        if n == 1:
            altre = f"Risulta inoltre scaduta e non saldata 1 altra fattura per {fmt_eur(total)}."
        else:
            altre = f"Risultano inoltre scadute e non saldate altre {n} fatture per un totale di {fmt_eur(total)}."
        paragraphs.append(f"{altre} Totale da saldare: {fmt_eur(grand_total)}. Riepilogo in allegato.")
    saldo = f"Le chiediamo di provvedere al saldo: bonifico a {config.COMPANY_NAME}"
    if config.COMPANY_IBAN:
        saldo += f", IBAN {config.COMPANY_IBAN}"
    if len(invoices) == 1:
        saldo += f", causale \"Fattura {numbers[0]}\"."
    elif len(invoices) <= MAX_NUMBERS_IN_CAUSALE:
        saldo += f", causale \"Fatture {_join_it(numbers)}\"."
    else:
        saldo += ", indicando nella causale i numeri delle fatture."
    paragraphs.append(saldo)
    paragraphs.append(
        "Se ha già effettuato il pagamento, Le chiediamo di inviarci la contabile "
        "rispondendo a questa email."
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
        "lines": lines,
        "overdue_count": len(overdue),
        "overdue_total": total,
        "grand_total": grand_total,
    }


def _pdf_view(invoice: Invoice, today: date) -> SimpleNamespace:
    """La fattura come la leggono i generatori di PDF, con i giorni di
    ritardo calcolati ad OGGI: sul documento che va al cliente non deve
    comparire un ritardo vecchio (o «in scadenza» su una fattura scaduta)."""
    return SimpleNamespace(
        invoice_number=invoice.invoice_number, amount=invoice.amount, amount_due=invoice.amount_due,
        issue_date=invoice.issue_date, due_date=invoice.due_date, status=invoice.status,
        source_platform=invoice.source_platform, days_overdue=days_overdue_on(invoice, today),
    )


def build_followup_attachments(customer: Customer, invoices: List[Invoice], overdue: List[Invoice],
                               today: Optional[date] = None) -> List[Tuple[str, bytes]]:
    """Una copia di cortesia per OGNI fattura dell'email + (se il cliente ha
    altre scadute) il riepilogo di tutte, quelle dell'email comprese, coi
    giorni di ritardo e il totale contati dalla stessa data del testo. Import
    locale: i generatori vivono in backend.api.recovery."""
    from backend.api.recovery import _build_invoice_pdf, _build_riepilogativo_pdf

    today = today or italian_today()
    invoices = _in_email_order(invoices)
    out = [
        (
            f"copia_cortesia_fattura_{safe_filename(invoice_label(inv.invoice_number))}.pdf",
            bytes(_build_invoice_pdf(customer, _pdf_view(inv, today), courtesy=True)),
        )
        for inv in invoices
    ]
    if overdue:
        views = [_pdf_view(i, today) for i in list(invoices) + list(overdue)]
        out.append((
            f"riepilogo_scadute_{safe_filename(customer.ragione_sociale or 'cliente')}.pdf",
            bytes(_build_riepilogativo_pdf(customer, views, title="Riepilogo Fatture Scadute")),
        ))
    return out


# ── Registro: le righe della stessa email ─────────────────────────────

def group_key(customer_id: int, invoices: Iterable[Invoice], today: date) -> str:
    """Identità di UNA email: cliente, giorno, fatture (con la scadenza). La
    stessa email ritentata lo stesso giorno ha la stessa chiave — è anche la
    Idempotency-Key verso Resend — mentre un insieme diverso di fatture o un
    altro giorno (testo diverso: i giorni di ritardo cambiano) ne ha un'altra."""
    parts = ",".join(
        f"{inv.id}:{inv.due_date.isoformat() if inv.due_date else ''}"
        for inv in sorted(invoices, key=lambda i: i.id)
    )
    digest = hashlib.sha256(f"{customer_id}|{parts}".encode("utf-8")).hexdigest()[:16]
    return f"c{customer_id}-{today.strftime('%Y%m%d')}-{digest}"


def group_members(session: Session, rows: Iterable[InvoiceFollowupEmail]) -> Dict[str, List[dict]]:
    """Per ogni `group_key` delle righe date, le fatture della stessa email
    (dalla scadenza più vecchia): {chiave: [{row_id, invoice_id, invoice_number, due_date, amount_due}]}."""
    keys = {r.group_key for r in rows if getattr(r, "group_key", None)}
    out: Dict[str, List[dict]] = {}
    if not keys:
        return out
    found = (
        session.query(InvoiceFollowupEmail, Invoice)
        .outerjoin(Invoice, Invoice.id == InvoiceFollowupEmail.invoice_id)
        .filter(InvoiceFollowupEmail.group_key.in_(keys))
        .order_by(InvoiceFollowupEmail.due_date.asc(), InvoiceFollowupEmail.id.asc())
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


def row_invoices(session: Session, row: InvoiceFollowupEmail) -> List[Invoice]:
    """Le fatture dell'email a cui appartiene la riga (lei compresa), per
    rigenerare testo e allegati: quelle ancora presenti nel DB."""
    ids = {row.invoice_id}
    if row.group_key:
        ids |= {
            r.invoice_id for r in
            session.query(InvoiceFollowupEmail).filter(InvoiceFollowupEmail.group_key == row.group_key).all()
        }
    return _in_email_order(session.query(Invoice).filter(Invoice.id.in_(ids)).all())


def _item(row: InvoiceFollowupEmail, invoice: Invoice, customer: Optional[Customer], group_size: int = 1) -> dict:
    item = base._item(row, invoice, customer)
    item["group_key"] = row.group_key
    item["group_size"] = group_size
    return item


# ── Anteprima ─────────────────────────────────────────────────────────

def _invoice_entry(session: Session, inv: Invoice, today: date, early: bool = False) -> dict:
    reminder = reminder_sent(session, inv)
    return {
        "invoice_id": inv.id,
        "invoice_number": inv.invoice_number,
        "amount_due": float(inv.amount_due or 0),
        "due_date": inv.due_date.isoformat(),
        "days_overdue": days_overdue_on(inv, today),
        "reminder_sent_at": reminder.sent_at.isoformat() if reminder is not None and reminder.sent_at else None,
        # Unita all'email di oggi perché scade il giorno dopo un'altra.
        "early": early,
    }


def _entry(session: Session, customer: Optional[Customer], invoices: List[Invoice], today: date, cfg,
           eval_mode: str, early_ids: Iterable[int] = ()) -> dict:
    """Una riga dell'anteprima: una email (una o più fatture) oppure una
    fattura che oggi non parte, col motivo."""
    early_ids = set(early_ids)
    first = invoices[0]
    items = [_invoice_entry(session, inv, today, early=inv.id in early_ids) for inv in invoices]
    amount = round(sum(i["amount_due"] for i in items), 2)
    return {
        "invoice_id": first.id,
        "invoice_number": first.invoice_number,
        "invoices": items,
        "invoice_count": len(items),
        "customer_id": customer.id if customer else None,
        "ragione_sociale": customer.ragione_sociale if customer else first.customer_name_raw,
        "to_email": (customer.email or "").strip() if customer else None,
        "cc": carbon_copies(customer, cfg) if (customer and eval_mode == MODE_REALE) else [],
        "would_cc": carbon_copies(customer, cfg) if customer else [],
        "amount_due": amount,
        "due_date": items[0]["due_date"],
        "days_overdue": items[0]["days_overdue"],
        "reminder_sent_at": items[0]["reminder_sent_at"],
        "skip_reason": None,
        "skip_label": None,
        "already": None,
        "already_mode": None,
        "will_process": False,
        "why": None,
        "overdue_count": 0,
        "overdue_total": 0.0,
        "grand_total": amount,
        "subject": None,
        "text": None,
    }


def preview_followups(session: Session, *, now_utc: Optional[datetime] = None, cfg=None,
                      mode: Optional[str] = None) -> List[dict]:
    """Cosa farebbe il giro di oggi: NESSUNA scrittura. Una riga per ogni
    EMAIL che partirebbe (con tutte le sue fatture) e una per ogni fattura in
    finestra che oggi non parte, col motivo. Da spento mostra ciò che
    partirebbe accendendolo (valutato con la modalità del promemoria)."""
    cfg = cfg or config
    window = followup_window(now_utc, cfg)
    if window is None:
        return []
    mode = mode or resolve_followup_mode(cfg)[0]
    eval_mode = resolve_mode(cfg)[0] if mode == MODE_OFF else mode
    today = italian_today(now_utc)
    out = []
    for invoices in by_customer(candidate_invoices(session, window)):
        plan = plan_customer(session, invoices, window, eval_mode)
        customer = plan.customer
        if plan.email:
            entry = _entry(session, customer, plan.email, today, cfg, eval_mode, plan.early_ids)
            overdue = other_overdue_invoices(session, customer.id, exclude_ids=[i.id for i in plan.email])
            msg = compose_followup(customer, plan.email, overdue, today)
            # Se una delle fatture ha già una riga (prova, test, fallita che
            # si ritenta) l'anteprima dice perché riparte.
            redo = next((plan.existing[i.id] for i in plan.email if i.id in plan.existing), None)
            entry.update({
                "will_process": True,
                "why": plan.why.get(redo.invoice_id) if redo is not None else "nuova",
                "already": redo.status if redo is not None else None,
                "already_mode": redo.mode if redo is not None else None,
                "overdue_count": msg["overdue_count"],
                "overdue_total": msg["overdue_total"],
                "grand_total": msg["grand_total"],
                "subject": msg["subject"],
                "text": msg["text"],
            })
            out.append(entry)
        for inv, reason in plan.skipped:
            entry = _entry(session, customer, [inv], today, cfg, eval_mode)
            entry.update({"skip_reason": reason, "skip_label": SKIP_LABELS.get(reason),
                          "why": SKIP_LABELS.get(reason, reason)})
            row = plan.existing.get(inv.id)
            if row is not None:
                entry.update({"already": row.status, "already_mode": row.mode})
            out.append(entry)
        for inv in plan.out_of_scope:
            entry = _entry(session, customer, [inv], today, cfg, eval_mode)
            reason = "senza_promemoria"
            entry.update({"skip_reason": reason, "skip_label": SKIP_LABELS.get(reason),
                          "why": SKIP_LABELS.get(reason, reason)})
            out.append(entry)
        for inv, row, why in plan.already:
            entry = _entry(session, customer, [inv], today, cfg, eval_mode)
            entry.update({"already": row.status, "already_mode": row.mode, "why": why})
            out.append(entry)
    return out


# ── Esecuzione ────────────────────────────────────────────────────────

def _summary(mode: str, missing: List[str], window: Optional[Tuple[date, date]]) -> dict:
    """Esito di un giro. `sent`, `prepared` e `failed` contano le EMAIL;
    `invoices` le fatture coperte dalle email inviate o preparate."""
    summary = base._empty_summary(None, mode, missing)
    summary.update({
        "kind": KIND_KEY,
        "window": [d.isoformat() for d in window] if window else None,
        "merge_until": merge_until(window).isoformat() if window else None,
        # Fatture coperte dalle email inviate o preparate.
        "invoices": 0,
        # Di cui unite perché scadono il giorno dopo un'altra dello stesso cliente.
        "merged_early": 0,
        # Fatture nella finestra ma senza promemoria inviato: non lavorate.
        "out_of_scope": 0,
        # Fatture saldate/annullate mentre il giro era in corso: non lavorate.
        "settled_meanwhile": 0,
        "blocked": None,
        "blocked_label": None,
        "sync": None,
    })
    return summary


def _blocked(summary: dict, why: str) -> dict:
    summary["blocked"] = why
    summary["blocked_label"] = BLOCK_LABELS.get(why, why)
    return summary


def _record_skip(session: Session, inv: Invoice, row: Optional[InvoiceFollowupEmail], reason: str,
                 mode: str, summary: dict) -> None:
    """Registra una fattura in perimetro ma esclusa, col motivo (mai scartata
    in silenzio). Un errore qui non ferma il giro."""
    try:
        customer = inv.customer
        row = row or InvoiceFollowupEmail(invoice_id=inv.id, due_date=inv.due_date)
        row.customer_id = customer.id if customer else None
        row.mode = mode
        row.amount_due = float(inv.amount_due or 0)
        row.error = None
        row.error_kind = None
        row.status = STATUS_SKIPPED
        row.skip_reason = reason
        row.group_key = None
        row.to_email = ((customer.email or "").strip() or None) if customer else None
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            summary["already"] += 1
            return
        summary["skipped"] += 1
        summary["items"].append(_item(row, inv, customer))
    except Exception as e:
        logger.error(f"{KIND_LABEL} {inv.invoice_number}: impossibile registrare l'esclusione: {e}", exc_info=True)
        try:
            session.rollback()
        except Exception:
            pass


def _process_group(session: Session, customer: Customer, invoices: List[Invoice], *, key: str, mode: str,
                   mailer, cfg, summary: dict, existing: Dict[int, InvoiceFollowupEmail], today: date) -> None:
    """Compone, registra e (fuori dalla prova) invia UNA email per le fatture
    date dello stesso cliente. Una riga per fattura, tutte con lo stesso
    `group_key`, riservate insieme PRIMA dell'invio e chiuse insieme dopo:
    le fatture di un'email hanno sempre lo stesso esito."""
    invoices = _in_email_order(invoices)
    overdue = other_overdue_invoices(session, customer.id, exclude_ids=[i.id for i in invoices])
    msg = compose_followup(customer, invoices, overdue, today)
    real_to = normalize_email(customer.email)
    test_to = (getattr(cfg, "REMINDER_EMAIL_TEST_TO", "") or "").strip()
    to_email = test_to if mode == MODE_TEST else real_to
    # Copie (commerciale + fatture@) solo nell'invio REALE: in test tutto va
    # al solo indirizzo di prova.
    cc = carbon_copies(customer, cfg) if mode == MODE_REALE else []
    attempted_at = datetime.utcnow()
    customer_id = customer.id
    facts = [(inv.id, inv.due_date, float(inv.amount_due or 0)) for inv in invoices]

    # Riserva PRIMA di fare qualsiasi cosa costosa o irreversibile: tutte le
    # righe dell'email passano a `sending` in UNA scrittura. Se un altro giro
    # ha già riservato anche una sola di queste fatture il vincolo unico fa
    # fallire l'intera riserva e l'email non parte.
    rows: List[InvoiceFollowupEmail] = []
    try:
        with session.no_autoflush:
            for invoice_id, due, amount_due in facts:
                row = existing.get(invoice_id) or InvoiceFollowupEmail(invoice_id=invoice_id, due_date=due)
                row.customer_id = customer_id
                row.mode = mode
                row.amount_due = amount_due
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
                row.group_key = key
                row.status = STATUS_SENDING
                row.attempted_at = attempted_at
                session.add(row)
                rows.append(row)
        session.commit()
    except IntegrityError:
        session.rollback()
        summary["already"] += len(invoices)
        return

    def _close() -> None:
        for row in rows:
            session.add(row)
        session.commit()
        for row, inv in zip(rows, invoices):
            summary["items"].append(_item(row, inv, customer, group_size=len(rows)))

    try:
        attachments = build_followup_attachments(customer, invoices, overdue, today)
    except Exception as e:  # il PDF non deve far cadere il giro
        logger.error(f"{KIND_LABEL} {customer.id}: PDF non generato: {e}", exc_info=True)
        for row in rows:
            row.status = STATUS_FAILED
            row.error_kind = ERROR_PDF
            row.error = f"PDF non generato: {e}"[:500]
        summary["failed"] += 1
        _close()
        return
    names = [name for name, _ in attachments]
    for row in rows:
        row.attachments_json = names

    if mode == MODE_PROVA:
        for row in rows:
            row.status = STATUS_PREPARED
        summary["prepared"] += 1
        summary["invoices"] += len(rows)
    else:
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
                idempotency_key=f"{KIND_KEY}-{key}-{mode}",
            )
            sent_at = datetime.utcnow()
            for row in rows:
                row.status = STATUS_SENT
                row.sent_at = sent_at
                row.provider_message_id = message_id or None
            summary["sent"] += 1
            summary["invoices"] += len(rows)
        except Exception as e:  # messaggio già privo di segreti (MailSendError)
            logger.error(f"{KIND_LABEL} cliente {customer.id}: invio fallito: {e}")
            for row in rows:
                row.status = STATUS_FAILED
                row.error_kind = base._classify_error(e)
                row.error = str(e)[:500]
            summary["failed"] += 1
    _close()


def _process_group_safely(session: Session, customer: Customer, invoices: List[Invoice], **kwargs) -> None:
    """Un cliente non deve mai far saltare gli altri: eccezioni impreviste →
    rollback, righe `failed` (other: ferme finché un operatore non le
    ritenta) se possibile, e si prosegue."""
    summary = kwargs["summary"]
    ids = [(inv.id, inv.due_date, inv.customer_id, float(inv.amount_due or 0), inv) for inv in invoices]
    try:
        _process_group(session, customer, invoices, **kwargs)
    except Exception as e:
        logger.error(f"{KIND_LABEL} cliente {ids[0][2] if ids else '?'}: errore imprevisto: {e}", exc_info=True)
        try:
            session.rollback()
            rows = []
            for invoice_id, due, customer_id, amount_due, inv in ids:
                row = session.query(InvoiceFollowupEmail).filter_by(invoice_id=invoice_id, due_date=due).first()
                if row is None:
                    row = InvoiceFollowupEmail(invoice_id=invoice_id, due_date=due, customer_id=customer_id,
                                               mode=kwargs["mode"], amount_due=amount_due)
                row.status = STATUS_FAILED
                row.error_kind = ERROR_OTHER
                row.error = f"Errore imprevisto: {e}"[:500]
                row.group_key = kwargs["key"]
                session.add(row)
                rows.append((row, inv))
            session.commit()
            summary["failed"] += 1
            for row, inv in rows:
                summary["items"].append(_item(row, inv, inv.customer, group_size=len(rows)))
        except Exception as inner:
            logger.error(f"{KIND_LABEL}: impossibile registrare l'errore: {inner}")
            try:
                session.rollback()
            except Exception:
                pass


def _run_customer(session: Session, invoices: List[Invoice], window: Tuple[date, date], *, mode: str, mailer,
                  cfg, summary: dict, today: date) -> None:
    """Il giro per UN cliente: al massimo una email, con tutte le sue fatture
    pronte. Lo stato delle fatture viene letto ADESSO (dopo ogni commit gli
    oggetti sono ricaricati dal DB): un sync che ne segna una pagata a giro
    in corso la toglie dall'email."""
    plan = plan_customer(session, invoices, window, mode)
    summary["settled_meanwhile"] += len(plan.settled)
    summary["out_of_scope"] += len(plan.out_of_scope)
    summary["candidates"] += plan.in_scope + len(plan.early_ids)
    summary["already"] += len(plan.already)
    skipped = [(inv, plan.existing.get(inv.id), reason) for inv, reason in plan.skipped]
    if plan.email:
        # Prima l'email (nessuna scrittura tra la lettura dello stato e la
        # riserva), poi le esclusioni.
        summary["merged_early"] += len(plan.early_ids)
        _process_group_safely(
            session, plan.customer, plan.email, key=group_key(plan.customer.id, plan.email, today),
            mode=mode, mailer=mailer, cfg=cfg, summary=summary, existing=plan.existing, today=today,
        )
    for inv, row, reason in skipped:
        _record_skip(session, inv, row, reason, mode, summary)


def run_followups(
    session: Session,
    *,
    now_utc: Optional[datetime] = None,
    mailer=None,
    cfg=None,
    mode: Optional[str] = None,
    expected_mode: Optional[str] = None,
) -> dict:
    """Il giro quotidiano del secondo avviso: una email per cliente. Non
    parte (e non scrive nulla) se la funzione è spenta, nel weekend o se il
    sync delle fatture non è aggiornato: il motivo è in `blocked`.

    `expected_mode`: la modalità che l'operatore ha confermato a video; se il
    server ne risolve un'altra (env cambiate nel frattempo) non si parte.
    """
    cfg = cfg or config
    resolved, missing = resolve_followup_mode(cfg)
    mode = mode or resolved
    if expected_mode and expected_mode != mode:
        raise ModeMismatch(mode, expected_mode)
    window = followup_window(now_utc, cfg)
    summary = _summary(mode, missing, window)
    if mode == MODE_OFF:
        return _blocked(summary, "off")
    if window is None:
        return _blocked(summary, "weekend")
    gate = sync_gate(session, now_utc, cfg)
    summary["sync"] = gate
    if not gate["ok"]:
        logger.warning("Secondo avviso email NON eseguito: %s", gate["detail"])
        return _blocked(summary, "sync")

    if not base._reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, _missing, mailer, own_mailer = base._resolve_runtime(cfg, mailer, mode)
        today = italian_today(now_utc)
        try:
            for invoices in by_customer(candidate_invoices(session, window)):
                _run_customer(session, invoices, window, mode=mode, mailer=mailer, cfg=cfg,
                              summary=summary, today=today)
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        base._reminder_lock.release()

    logger.info(
        "Secondo avviso email (scadenze %s → %s, unite fino al %s, modalità %s): candidate=%d "
        "email_inviate=%d email_preparate=%d fatture_nelle_email=%d di_cui_unite=%d saltate=%d "
        "email_fallite=%d non_lavorate=%d senza_promemoria=%d",
        summary["window"][0], summary["window"][1], summary["merge_until"], mode, summary["candidates"],
        summary["sent"], summary["prepared"], summary["invoices"], summary["merged_early"], summary["skipped"],
        summary["failed"], summary["already"], summary["out_of_scope"],
    )
    return summary


def _retry_members(session: Session, row: InvoiceFollowupEmail,
                   today: date) -> List[Tuple[Invoice, InvoiceFollowupEmail]]:
    """Le ALTRE righe della stessa email ancora ferme (fallite o interrotte)
    e ancora da sollecitare: si ritentano insieme alla riga scelta, così il
    cliente riceve di nuovo UNA email e non una per fattura. Una fattura nel
    frattempo saldata, con la scadenza cambiata o non più scaduta resta
    fuori (la sua riga non cambia)."""
    if not row.group_key:
        return []
    others = (
        session.query(InvoiceFollowupEmail)
        .filter(
            InvoiceFollowupEmail.group_key == row.group_key,
            InvoiceFollowupEmail.id != row.id,
            InvoiceFollowupEmail.customer_id == row.customer_id,
            InvoiceFollowupEmail.status.in_((STATUS_FAILED, STATUS_SENDING)),
        )
        .order_by(InvoiceFollowupEmail.id.asc())
        .all()
    )
    out = []
    for other in others:
        inv = session.query(Invoice).filter(Invoice.id == other.invoice_id).first()
        if inv is None or not still_due(inv):
            continue
        if inv.due_date is None or inv.due_date != other.due_date or inv.due_date >= today:
            continue
        out.append((inv, other))
    return out


def retry_followup_row(session: Session, row: InvoiceFollowupEmail, *, mailer=None, cfg=None,
                       mode: Optional[str] = None, expected_mode: Optional[str] = None,
                       now_utc: Optional[datetime] = None) -> dict:
    """Ritenta UN secondo avviso fermo (`failed` o `sending` orfana), insieme
    alle altre fatture della stessa email ancora ferme. La fattura deve
    essere ancora scaduta e non saldata, con la stessa scadenza; valgono lo
    spento e il controllo del sync del giro."""
    cfg = cfg or config
    if row.status not in (STATUS_FAILED, STATUS_SENDING):
        raise ValueError("si ritentano solo gli avvisi falliti o interrotti")
    inv = session.query(Invoice).filter(Invoice.id == row.invoice_id).first()
    if inv is None:
        raise ValueError("fattura non più presente")
    if not still_due(inv):
        raise ValueError("fattura non più da sollecitare (pagata, annullata o contestata)")
    today = italian_today(now_utc)
    if inv.due_date is None or inv.due_date != row.due_date:
        raise ValueError("la scadenza della fattura è cambiata: questo avviso non è più valido")
    if inv.due_date >= today:
        raise ValueError("la fattura non è scaduta")
    resolved, missing = resolve_followup_mode(cfg)
    mode = mode or resolved
    if expected_mode and expected_mode != mode:
        raise ModeMismatch(mode, expected_mode)
    if mode == MODE_OFF:
        raise ValueError("secondo avviso spento (REMINDER_FOLLOWUP_ENABLED)")
    gate = sync_gate(session, now_utc, cfg)
    if not gate["ok"]:
        raise ValueError(f"sync fatture non aggiornato ({gate['detail']}): riprova dopo un sync riuscito")
    if not base._reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, _missing, mailer, own_mailer = base._resolve_runtime(cfg, mailer, mode)
        summary = _summary(mode, missing, None)
        summary["sync"] = gate
        members = [(inv, row)] + _retry_members(session, row, today)
        summary["candidates"] = len(members)
        try:
            ready: List[Invoice] = []
            skipped = []
            existing: Dict[int, InvoiceFollowupEmail] = {}
            for member, member_row in members:
                reason = followup_skip_reason(session, member, member.customer)
                if reason:
                    skipped.append((member, member_row, reason))
                    continue
                ready.append(member)
                existing[member.id] = member_row
            if ready:
                customer = inv.customer
                _process_group_safely(
                    session, customer, ready, key=group_key(customer.id, ready, today), mode=mode,
                    mailer=mailer, cfg=cfg, summary=summary, existing=existing, today=today,
                )
            for member, member_row, reason in skipped:
                _record_skip(session, member, member_row, reason, mode, summary)
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        base._reminder_lock.release()
    return summary


def email_group_of(session: Session, invoice: Invoice, *, now_utc: Optional[datetime] = None,
                   cfg=None) -> List[Invoice]:
    """Le fatture dell'email che il cliente riceverebbe OGGI per la fattura
    data: l'email del giro di oggi se la fattura ne fa parte (la stessa riga
    dell'anteprima), altrimenti la fattura da sola."""
    cfg = cfg or config
    window = followup_window(now_utc, cfg)
    if window is None:
        return [invoice]
    candidates = candidate_invoices(session, window, customer_id=invoice.customer_id)
    plan = plan_customer(session, candidates, window, resolve_mode(cfg)[0], ignore_email=True)
    if any(inv.id == invoice.id for inv in plan.email):
        return plan.email
    return [invoice]


def send_followup_test(session: Session, invoice_id: int, to_address: str, *, mailer=None, cfg=None,
                       now_utc: Optional[datetime] = None) -> dict:
    """Invia a UN indirizzo interno il secondo avviso che il cliente
    riceverebbe per la fattura data — l'email unica con le altre sue fatture
    del giro di oggi, se ce ne sono ([PROVA], nessuna copia, registro
    intoccato). Funziona anche da spento: serve a leggere testo e allegati
    veri prima di accenderlo. Richiede Resend configurato."""
    cfg = cfg or config
    to = normalize_email(to_address)
    if not to:
        raise ValueError("indirizzo di prova non valido")
    inv = session.query(Invoice).filter(Invoice.id == invoice_id).first()
    if inv is None:
        raise NotFound("fattura non trovata")
    if inv.customer is None:
        raise ValueError("fattura senza cliente")
    if not still_due(inv):
        raise ValueError("fattura pagata, annullata o contestata")
    today = italian_today(now_utc)
    if inv.due_date is None or inv.due_date >= today:
        raise ValueError("fattura non ancora scaduta: il secondo avviso riguarda solo fatture scadute")
    customer = inv.customer
    reason = followup_skip_reason(session, inv, customer, ignore_email=True)
    if reason:
        raise ValueError(f"secondo avviso non inviabile: {SKIP_LABELS.get(reason, reason)}")
    # La prova va a un indirizzo INTERNO: mai a un cliente (nemmeno per errore
    # di copia-incolla dall'anteprima). Stessa regola del promemoria.
    is_customer_email = session.query(Customer.id).filter(
        func.lower(func.trim(Customer.email)) == to
    ).first() is not None
    if is_customer_email or to == normalize_email(customer.email):
        raise ValueError("l'indirizzo di prova coincide con l'email di un cliente: usare un indirizzo interno")
    if not getattr(cfg, "RESEND_API_KEY", ""):
        raise ValueError("Resend non configurato (RESEND_API_KEY)")
    invoices = email_group_of(session, inv, now_utc=now_utc, cfg=cfg)
    overdue = other_overdue_invoices(session, customer.id, exclude_ids=[i.id for i in invoices])
    msg = compose_followup(customer, invoices, overdue, today)
    attachments = build_followup_attachments(customer, invoices, overdue, today)
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
    logger.info("%s di PROVA inviato per %d fatture del cliente %s", KIND_LABEL, len(invoices), customer.id)
    return {
        "sent_to": to, "invoice_id": inv.id, "invoice_number": inv.invoice_number,
        "invoice_numbers": [i.invoice_number for i in invoices],
        "ragione_sociale": customer.ragione_sociale, "subject": f"[PROVA] {msg['subject']}",
        "attachments": [name for name, _ in attachments], "provider_message_id": message_id or None,
        "would_cc": carbon_copies(customer, cfg),
    }
