"""Stato delle email automatiche accanto al nome del cliente e registro unico
«Email inviate» (richiesta owner 24–26/11/2026: «deve essere facile capire lo
stato della segnalazione, per evitare la over segnalazione ma anche di perdere
segnalazioni per strada»; «a Luigi serve sapere quali mail sono partite e
quando»).

SOLA LETTURA dei due registri (promemoria pre-scadenza e secondo avviso): qui
non si invia, non si scrive e non si decide nulla.

Ogni frase mostrata deve essere vera PER COSTRUZIONE, qualunque cosa sia
successa attorno. Quindi:

- contano solo le righe in modalità REALE (una prova o un test non sono
  arrivati al cliente) e solo quelle del cliente a cui la fattura appartiene
  ancora (una scheda unita a un'altra è lo stesso cliente);
- accanto al nome si parla delle sole fatture ANCORA DA SALDARE; il registro
  «Email inviate» elenca ciò che è successo, giorno per giorno;
- «inviato» si dice solo di una riga inviata; «non partito» solo di ciò che
  CERTAMENTE non è partito (per il promemoria: mai tentato), col motivo
  scritto dal giro detto come un fatto passato, senza confrontarlo con
  l'oggi; finché un giro può ancora mandarlo si dice «non ancora partito»;
  di un invio tentato e non concluso si dice «esito incerto» — per il
  promemoria sempre, perché il suo registro ricorda solo l'ultimo errore e
  un «Riprova» può riscriverlo;
- «ferma, da gestire» solo ciò che «Riprova» in Sistema accetta;
- del futuro si dicono due sole cose, e solo a secondo avviso acceso in
  reale: il giorno del prossimo giro IN PROGRAMMA che prenderà la fattura
  («previsto»), e ciò che il giro farebbe adesso («sospeso», «non previsto»:
  è la sua stessa regola, valutata ora). All'ultimo giorno utile, passata
  l'ora del giro, non si promette e non si nega: «non ancora partito»;
- i nomi sono quelli delle email («Promemoria», «2° avviso»), mai «prima /
  seconda segnalazione»: in Attività I e II contatto sono già le segnalazioni
  fatte a mano dall'operatore.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, not_
from sqlalchemy.orm import Session, defer, joinedload

from backend.config import config
from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail
from backend.engine import followup_email as followup
from backend.engine.agents import agents_of
from backend.engine.format import italian_today
from backend.engine.reminder_email import (
    MODE_REALE, NOT_REMINDABLE_STATUSES, STATUS_FAILED, STATUS_SENDING, STATUS_SENT, STATUS_SKIPPED,
    WEEKDAYS_IT, invoice_label, target_due_dates,
)

logger = logging.getLogger(__name__)

KIND_REMINDER = "promemoria"
KIND_FOLLOWUP = followup.KIND_KEY
KIND_LABELS = {KIND_REMINDER: "Promemoria", KIND_FOLLOWUP: "2° avviso"}

# Come si presenta una voce (un colore = un significato).
SENT = "sent"          # verde: arrivata al cliente
PLANNED = "planned"    # grigio vuoto: un giro in programma la manderà (o la ritenterà)
STUCK = "stuck"        # ambra: serve un operatore, e da Sistema può farlo
MISSING = "missing"    # rosso: c'è un dato da sistemare, o l'esito è incerto
PAUSED = "paused"      # viola: ADESSO il giro la salterebbe per una scelta o uno stato della pratica
CLOSED = "closed"      # grigio pieno: non partita, un fatto passato che non chiede nulla

STUCK_STATUSES = (STATUS_FAILED, STATUS_SENDING)
LOG_STATUSES = (STATUS_SENT, STATUS_FAILED, STATUS_SENDING, STATUS_SKIPPED)

# Il motivo in poche parole, come sta accanto a un nome.
REASON_SHORT = {
    "senza_cliente": "fattura senza cliente",
    "cliente_unito": "scheda unita a un'altra",
    "cliente_escluso": "cliente escluso",
    "avvocato": "pratica dall'avvocato",
    "in_incasso": "assegno in incasso",
    "scadenza_stimata": "scadenza stimata",
    "forse_pagata": "forse già pagata",
    "abbinamento_da_verificare": "abbinamento da verificare",
    "senza_email": "senza email",
    "email_non_valida": "email non valida",
    "senza_promemoria": "promemoria non inviato",
    "in_attesa": "in attesa",
    "promessa": "pagamento promesso",
}
# Motivi che NON chiedono di sistemare nulla: una scelta dell'operatore o uno
# stato della pratica. Ogni altro motivo (anche uno nuovo, non ancora in
# elenco) è «da sistemare»: nel dubbio si segnala.
PAUSE_REASONS = frozenset((
    "cliente_unito", "cliente_escluso", "avvocato", "in_incasso", "forse_pagata", "in_attesa", "promessa",
))

# Il giro del mattino parte all'ora in programma, ma può aspettare qualche
# minuto un giro avviato a mano e impiega il suo tempo: per questo margine
# «oggi» resta il giorno del giro (salvo prova contraria: una riga tentata
# oggi dopo l'ora del giro dice che per lei il giro di oggi è già passato).
ROUND_GRACE_MINUTES = 10
# Una riga «in invio» da meno di così è un invio IN CORSO, non una riga ferma.
IN_FLIGHT_MINUTES = 3

MIN_LOG_DAYS = 7
MAX_LOG_DAYS = 90
MAX_TITLE_LINES = 6

ITEM_ORDER = (
    (KIND_REMINDER, SENT), (KIND_REMINDER, STUCK), (KIND_REMINDER, MISSING), (KIND_REMINDER, CLOSED),
    (KIND_FOLLOWUP, SENT), (KIND_FOLLOWUP, STUCK), (KIND_FOLLOWUP, PLANNED), (KIND_FOLLOWUP, PAUSED),
    (KIND_FOLLOWUP, MISSING), (KIND_FOLLOWUP, CLOSED),
)


# ── Date ───────────────────────────────────────────────────────────

def _local(moment: Optional[datetime]) -> Optional[datetime]:
    """Un istante del registro (UTC senza fuso) nell'ora italiana."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(config.TIMEZONE))


def event_time(row) -> Optional[datetime]:
    """Quando è successo ciò che la riga racconta: l'invio per una riga
    inviata, l'ultimo tentativo (o la nascita della riga) per le altre."""
    if row.status == STATUS_SENT and row.sent_at is not None:
        return row.sent_at
    return row.attempted_at or row.created_at


def event_day(row) -> Optional[date]:
    """Il giorno ITALIANO di ciò che la riga racconta (None se non registrato)."""
    local = _local(event_time(row))
    return local.date() if local else None


def short_date(day: Optional[date], today: date) -> str:
    """gg/mm; con l'anno solo se non è quello in corso."""
    if day is None:
        return ""
    return day.strftime("%d/%m") if day.year == today.year else day.strftime("%d/%m/%y")


def reason_text(code: Optional[str]) -> str:
    return REASON_SHORT.get(code or "", (code or "motivo non registrato").replace("_", " "))


def reason_state(code: Optional[str]) -> str:
    """Ciò che il giro farebbe ADESSO per questo motivo: sospeso o da sistemare."""
    return PAUSED if code in PAUSE_REASONS else MISSING


def fact_state(code: Optional[str]) -> str:
    """Un mancato invio già avvenuto: un fatto e basta, o un dato da sistemare."""
    return CLOSED if code in PAUSE_REASONS else MISSING


def was_attempted(row) -> bool:
    """La riga ha avuto almeno un tentativo di invio? I due motori non
    azzerano `attempted_at` quando poi la riga diventa «saltata»."""
    return row.attempted_at is not None or row.status in STUCK_STATUSES


def round_minutes(cfg=None) -> int:
    """L'ora del giro in programma, in minuti dalla mezzanotte italiana."""
    cfg = cfg or config
    return int(getattr(cfg, "REMINDER_EMAIL_HOUR", 9)) * 60 + int(getattr(cfg, "REMINDER_EMAIL_MINUTE", 15))


def next_working_day(day: date) -> date:
    """Il primo giorno lavorativo DOPO `day`."""
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def start_after(row, today: date, start: date, cfg=None) -> date:
    """Il primo giro in programma che può ancora riprendere QUESTA riga: se è
    stata tentata oggi dall'ora del giro in poi, il giro di oggi per lei è
    già passato (qualunque cosa dica l'orologio col suo margine)."""
    local = _local(row.attempted_at)
    if local is not None and local.date() == today and local.hour * 60 + local.minute >= round_minutes(cfg):
        return max(start, next_working_day(today))
    return start


def in_flight(row, now: datetime) -> bool:
    """Riga «in invio» da pochissimo: il giro la sta mandando in questo
    momento. Non è ferma e non è incerta: tra un attimo dirà com'è andata."""
    if row.status != STATUS_SENDING or row.attempted_at is None:
        return False
    return timedelta(0) <= now - row.attempted_at < timedelta(minutes=IN_FLIGHT_MINUTES)


def first_round_day(now_utc: Optional[datetime] = None, cfg=None) -> date:
    """Il primo giorno il cui giro IN PROGRAMMA deve ancora partire: oggi, se
    è un giorno lavorativo e l'ora del giro (REMINDER_EMAIL_HOUR:MINUTE, ora
    italiana, più un margine) non è passata; altrimenti il prossimo giorno
    lavorativo. Dopo quell'ora un giro può ancora partire oggi (a mano, o in
    ritardo dopo un riavvio), ma non è in programma: non lo si promette."""
    local = _local(followup._utc_now(now_utc))
    day = local.date()
    if local.hour * 60 + local.minute >= round_minutes(cfg) + ROUND_GRACE_MINUTES:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def planned_day(due: Optional[date], start: date, cfg=None) -> Optional[date]:
    """Il primo giorno, da `start` in poi, in cui il giro del secondo avviso
    prende una fattura con questa scadenza: N giorni dopo la scadenza, mai nel
    weekend, entro i giorni di recupero. None se quella finestra è passata.
    È la stessa finestra di followup_window letta dalla parte della fattura."""
    if due is None:
        return None
    first = due + timedelta(days=followup.followup_days(cfg))
    last = first + timedelta(days=followup.followup_lookback(cfg))
    day = max(start, first)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day if day <= last else None


def reminder_can_still_leave(due: Optional[date], today: date) -> bool:
    """Un giro può ancora prendere un promemoria con questa scadenza? Il giro
    guarda solo le scadenze dei giorni dopo (reminder_email.target_due_dates:
    da lunedì a giovedì quella di domani, il venerdì fino a lunedì, mai nel
    weekend). Nel giorno della scadenza, e nel weekend che la precede, nessun
    giro la guarda più."""
    if due is None or due <= today:
        return False
    day = today
    while day < due and (day - today).days < 7:
        if due in target_due_dates(datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)):
            return True
        day += timedelta(days=1)
    return False


def reminder_retriable(row, today: date) -> bool:
    """Un promemoria non riuscito si può ancora riprendere da Sistema? Solo
    finché la scadenza non è passata (reminder_email.retry_row): dopo non è
    più un promemoria, e nessun giro lo manderà."""
    return row.due_date is not None and row.due_date >= today


# ── Clienti uniti ──────────────────────────────────────────────────

def merge_map(session: Session) -> Dict[int, int]:
    """{scheda unita: scheda a cui è stata unita}, letta una volta sola: il
    confronto «è lo stesso cliente?» si fa su centinaia di righe e non deve
    costare una query ciascuna (le righe dei registri restano intestate alla
    scheda di allora anche dopo un'unione)."""
    found = session.query(Customer.id, Customer.merged_into).filter(Customer.merged_into.isnot(None)).all()
    return {customer_id: target for customer_id, target in found}


def same_customer(merged: Dict[int, int], reminded_id: Optional[int], current_id: Optional[int]) -> bool:
    """Stessa regola di followup._is_same_customer (stessa scheda, o scheda
    UNITA a quella attuale anche in più passaggi), sulla mappa già letta."""
    if reminded_id is None or current_id is None:
        return False
    seen = set()
    while reminded_id is not None and reminded_id not in seen:
        if reminded_id == current_id:
            return True
        seen.add(reminded_id)
        reminded_id = merged.get(reminded_id)
    return False


def survivor_id(merged: Dict[int, int], customer_id: Optional[int]) -> Optional[int]:
    """La scheda da aprire oggi: quella a cui il cliente è stato unito, se lo è stato."""
    seen = set()
    while customer_id is not None and customer_id in merged and customer_id not in seen:
        seen.add(customer_id)
        customer_id = merged[customer_id]
    return customer_id


# ── Lettura dei registri ───────────────────────────────────────────

def _with_customer():
    """La fattura col suo cliente e i nomi accettati, caricati insieme: le
    regole di esclusione li leggono, e non devono costare una query a cliente."""
    return joinedload(Invoice.customer).selectinload(Customer.accepted_names)


def _open_rows(session: Session, model, merged: Dict[int, int]) -> Dict[int, Tuple[Invoice, list]]:
    """Le righe REALI di fatture ancora da saldare, per fattura (dalla
    scadenza più vecchia): {id fattura: (fattura, righe)}. Solo righe del
    cliente a cui la fattura appartiene ora."""
    found = (
        session.query(model, Invoice)
        .join(Invoice, Invoice.id == model.invoice_id)
        # Il testo dell'email non serve qui: non va letto a ogni apertura di pagina.
        .options(_with_customer(), defer(model.body_text))
        .filter(
            model.mode == MODE_REALE,
            Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
            Invoice.amount_due > 0,
            Invoice.customer_id.isnot(None),
        )
        .order_by(model.due_date.asc(), model.id.asc())
        .all()
    )
    grouped: Dict[int, Tuple[Invoice, list]] = {}
    for row, inv in found:
        if not same_customer(merged, row.customer_id, inv.customer_id):
            continue
        grouped.setdefault(inv.id, (inv, []))[1].append(row)
    return grouped


def _current(rows: list, inv: Invoice):
    """La riga della scadenza che la fattura ha ADESSO (ce n'è al più una)."""
    return next((r for r in rows if r.due_date == inv.due_date), None)


def _safely(session: Session, read, fallback):
    """Il registro dei secondi avvisi non deve mai nascondere quello dei
    promemoria (stessa regola di /email/log): se non è leggibile si logga, si
    prosegue col solo promemoria e lo si DICE a chi guarda. (letto, leggibile)"""
    try:
        return read(), True
    except Exception as e:
        logger.error(f"Registro dei secondi avvisi non leggibile: {e}", exc_info=True)
        session.rollback()
        return fallback, False


def _item(kind: str, state: str, label: str, lines: Iterable[str], notes: Iterable[str] = (),
          day: Optional[date] = None, count: int = 1) -> dict:
    """Una voce sotto il nome. `lines`: una riga per fattura (tagliate se
    troppe); `notes`: le avvertenze, che non si tagliano mai."""
    lines = list(lines)
    if len(lines) > MAX_TITLE_LINES:
        lines = lines[:MAX_TITLE_LINES] + [f"… e altre {len(lines) - MAX_TITLE_LINES}"]
    return {
        "kind": kind, "state": state, "label": label, "title": "\n".join(lines + list(notes)),
        "date": day.isoformat() if day else None, "count": count,
    }


def _when_text(moment: Optional[datetime]) -> str:
    local = _local(moment)
    return local.strftime("il %d/%m/%Y alle %H:%M") if local else "in data non registrata"


def _counted(n: int, days: set, one: str, many: str, today: date) -> str:
    """«Promemoria 16/11», «2 promemoria 17/11» (stesso giorno), «2
    promemoria, ultimo 17/11» (giorni diversi)."""
    known = {d for d in days if d is not None}
    last = short_date(max(known), today) if known else ""
    if n == 1:
        return f"{one} {last}".strip()
    if len(known) <= 1:
        return f"{n} {many} {last}".strip()
    return f"{n} {many}, ultimo {last}"


def _latest(days: set) -> Optional[date]:
    known = [d for d in days if d is not None]
    return max(known) if known else None


def _email_key(row) -> str:
    return row.group_key or f"riga-{row.id}"


def _numbers(group: list) -> str:
    """«Fattura N» / «Fatture N, M» per le fatture di una stessa email."""
    numbers = ", ".join(invoice_label(inv.invoice_number) for inv, _row in group)
    return f"Fattur{'a' if len(group) == 1 else 'e'} {numbers}"


def _by_reason(pairs: List[Tuple[Invoice, Optional[str]]]) -> Dict[Optional[str], List[Invoice]]:
    grouped: Dict[Optional[str], List[Invoice]] = {}
    for inv, reason in pairs:
        grouped.setdefault(reason or None, []).append(inv)
    return grouped


def leave_days(pairs: List[Tuple[Invoice, date]], cfg=None, floors: Optional[Dict[int, date]] = None) -> Dict[int, date]:
    """{id fattura: giorno in cui parte davvero}. Una email per cliente: il
    giorno della prima porta con sé le fatture che quel giorno sono al più a
    un giorno dalla soglia (scadenza prossima); le altre fanno lo stesso tra
    loro, alla loro prima data. `pairs`: (fattura, il suo giorno da sola);
    `floors`: per le fatture che non possono partire prima di un certo giro
    (già tentate oggi), quel giorno."""
    reach = timedelta(days=followup.NEAR_DUE_DAYS) - timedelta(days=followup.followup_days(cfg))
    floors = floors or {}
    remaining = list(pairs)
    out: Dict[int, date] = {}
    while remaining:
        first = min(day for _inv, day in remaining)
        for inv, _own in remaining:
            if inv.due_date <= first + reach and first >= floors.get(inv.id, first):
                out[inv.id] = first
        remaining = [(inv, own) for inv, own in remaining if inv.id not in out]
    return out


# ── Promemoria accanto al nome ─────────────────────────────────────

def _reminder_items(entries: List[Tuple[Invoice, list]], today: date, now: datetime) -> List[dict]:
    """Promemoria pre-scadenza delle fatture ancora da saldare di un cliente.
    `now`: adesso, in UTC senza fuso (come le date del registro)."""
    reminded = reminded_ids(entries)
    sent: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    stuck: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    uncertain: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    skipped: List[Tuple[Invoice, Tuple[Optional[str], bool]]] = []
    for inv, rows in entries:
        current = _current(rows, inv)
        for r in rows:
            if r.status == STATUS_SENT:
                sent.append((inv, r))
            elif was_attempted(r) and not in_flight(r, now):
                # Tentato e non concluso. Il registro dei promemoria ricorda
                # solo l'ULTIMO errore, e un «Riprova» può riscrivere la riga
                # (anche come «saltata»): non si può mai dire «non partito».
                if r is current and r.status in STUCK_STATUSES and reminder_retriable(r, today):
                    stuck.append((inv, r))
                else:
                    uncertain.append((inv, r))
        if current is not None and current.status == STATUS_SKIPPED and not was_attempted(current):
            # Finché un giro può ancora guardare questa scadenza (quello in
            # programma, o uno più tardi lo stesso giorno) è «non ancora».
            skipped.append((inv, (current.skip_reason or None, reminder_can_still_leave(current.due_date, today))))
    items: List[dict] = []
    if sent:
        days = {event_day(r) for _i, r in sent}
        items.append(_item(
            KIND_REMINDER, SENT, _counted(len(sent), days, "Promemoria", "promemoria", today),
            (f"Fattura {invoice_label(i.invoice_number)}: promemoria inviato {_when_text(event_time(r))}"
             f"{' a ' + r.to_email if r.to_email else ''}" for i, r in sent),
            day=_latest(days), count=len(sent),
        ))
    if stuck:
        days = {event_day(r) for _i, r in stuck}
        items.append(_item(
            KIND_REMINDER, STUCK, _counted(len(stuck), days, "Promemoria fermo", "promemoria fermi", today),
            (f"Fattura {invoice_label(i.invoice_number)}: ultimo tentativo {_when_text(event_time(r))}, non concluso"
             for i, r in stuck),
            ["Da gestire in Sistema → «Promemoria email». Prima di ritentare, verificare su Resend che un "
             "tentativo precedente non sia già arrivato."],
            day=_latest(days), count=len(stuck),
        ))
    if uncertain:
        days = {event_day(r) for _i, r in uncertain}
        notes = ["Non si sa se il cliente l'ha ricevuto: si può verificare su Resend."]
        if any(i.id not in reminded for i, _r in uncertain):
            # Vero solo se per la scadenza di adesso non c'è un promemoria inviato.
            notes.append("Senza un promemoria inviato per la scadenza attuale il secondo avviso automatico non parte.")
        items.append(_item(
            KIND_REMINDER, MISSING,
            _counted(len(uncertain), days, "Promemoria dall'esito incerto", "promemoria dall'esito incerto", today),
            (f"Fattura {invoice_label(i.invoice_number)}: ultimo tentativo {_when_text(event_time(r))}, non concluso"
             f"{' (' + r.error[:120] + ')' if r.error else ''}" for i, r in uncertain),
            notes, day=_latest(days), count=len(uncertain),
        ))
    grouped: Dict[Tuple[Optional[str], bool], List[Invoice]] = {}
    for inv, key in skipped:
        grouped.setdefault(key, []).append(inv)
    for (reason, pending), invoices in grouped.items():
        n = len(invoices)
        words = reason_text(reason)
        if pending:
            label = f"Promemoria non ancora partito: {words}" if n == 1 else f"{n} promemoria non ancora partiti: {words}"
            notes = ["Se il motivo viene sistemato, un giro può ancora mandarlo entro il giorno prima della scadenza: "
                     "quello in programma se deve ancora partire, oppure uno avviato a mano («Esegui ora») o dopo un riavvio."]
        else:
            label = f"Promemoria non partito: {words}" if n == 1 else f"{n} promemoria non partiti: {words}"
            notes = ["È il motivo scritto dal giro quel giorno. Senza promemoria il secondo avviso automatico non parte."]
        items.append(_item(
            KIND_REMINDER, fact_state(reason), label,
            (f"Fattura {invoice_label(i.invoice_number)}: promemoria non inviato ({words})" for i in invoices),
            notes, count=n,
        ))
    return items


# ── Secondo avviso accanto al nome ─────────────────────────────────

def _attempt_text(row) -> str:
    """Un tentativo fermo del secondo avviso: è certo che non sia partito, o
    potrebbe essere arrivato? (il motore tiene distinta la cosa)"""
    kind = "dall'esito incerto, potrebbe essere arrivato" if followup._is_uncertain(row) else "non riuscito"
    return f"invio {kind} ({_when_text(event_time(row))})"


def reminded_ids(reminders: Iterable[Tuple[Invoice, list]]) -> set:
    """Le fatture che il giro del secondo avviso guarda (stessa regola di
    followup.reminder_sent): promemoria reale inviato per la scadenza di
    ADESSO, a questo cliente. `reminders`: [(fattura, sue righe già filtrate
    sul cliente)]."""
    return {
        inv.id for inv, rows in reminders
        if any(r.status == STATUS_SENT and r.due_date == inv.due_date for r in rows)
    }


def _followup_items(reminders: List[Tuple[Invoice, list]], followups: List[Tuple[Invoice, list]], today: date,
                    start: date, mode: str, cfg, now: datetime) -> List[dict]:
    """Secondo avviso delle fatture ancora da saldare di un cliente: ciò che
    è partito, ciò che è fermo e — solo da acceso in reale — ciò che il giro
    farà. `start`: il primo giorno il cui giro in programma deve ancora
    partire; `now`: adesso, in UTC senza fuso."""
    live = mode == MODE_REALE
    reminded = reminded_ids(reminders)
    first_useful = followup.followup_days(cfg)
    last_useful = first_useful + followup.followup_lookback(cfg)
    sent: Dict[str, list] = {}
    stuck: Dict[str, list] = {}
    orphans: Dict[str, list] = {}   # incerte che «Riprova» non accetta più
    retrying: List[Tuple[Invoice, date]] = []
    floors: Dict[int, date] = {}   # per chi è già stato tentato oggi: il primo giro che può riprenderlo
    planned: List[Tuple[Invoice, date]] = []
    last_day: List[Invoice] = []
    now_skips: List[Tuple[Invoice, Optional[str]]] = []    # ciò che il giro farebbe ADESSO
    written: Dict[int, Tuple[Invoice, Optional[str]]] = {}  # saltate: il motivo scritto dal giro
    expired: List[Invoice] = []
    blocked_ids = set()  # fatture la cui scadenza attuale ha già una riga che parla
    for inv, rows in followups:
        current = _current(rows, inv)
        for r in rows:
            if r.status == STATUS_SENT:
                sent.setdefault(_email_key(r), []).append((inv, r))
                continue
            if r.status not in STUCK_STATUSES:
                continue
            if in_flight(r, now):
                # Il giro la sta mandando adesso: non è ferma, e non va ripromessa.
                if r is current:
                    blocked_ids.add(inv.id)
                continue
            uncertain = followup._is_uncertain(r)
            # «Riprova» accetta solo la riga della scadenza attuale di una
            # fattura col suo promemoria (followup_email.retry_followup_row).
            if r is not current or inv.id not in reminded:
                if uncertain:
                    orphans.setdefault(_email_key(r), []).append((inv, r))
                continue  # certamente non partito e non più ritentabile: nulla da dire
            blocked_ids.add(inv.id)
            # Il giro riprende da solo ciò che CERTAMENTE non è partito, se ha
            # ancora un giro in programma nei giorni utili.
            floor = start_after(r, today, start, cfg)
            day = planned_day(inv.due_date, floor, cfg) if (live and not uncertain) else None
            if day is None:
                stuck.setdefault(_email_key(r), []).append((inv, r))
                continue
            reason = followup.exclusion(inv, inv.customer)
            if reason:
                now_skips.append((inv, reason))
            else:
                retrying.append((inv, day))
                floors[inv.id] = floor
        if current is None:
            continue
        if current.status == STATUS_SENT:
            blocked_ids.add(inv.id)
        elif current.status == STATUS_SKIPPED:
            written[inv.id] = (inv, current.skip_reason)

    if live:
        for inv, _rows in reminders:
            if inv.id in blocked_ids or inv.id not in reminded or inv.due_date is None or inv.due_date >= today:
                continue
            if planned_day(inv.due_date, today, cfg) is None:
                # Giorni utili passati: resta il motivo scritto, se c'è.
                if inv.id not in written:
                    expired.append(inv)
                continue
            # Nei giorni utili ogni giro rivaluta anche le saltate: conta ciò
            # che il giro farebbe ADESSO, non il motivo scritto ieri.
            written.pop(inv.id, None)
            reason = followup.exclusion(inv, inv.customer)
            if reason:
                now_skips.append((inv, reason))
                continue
            day = planned_day(inv.due_date, start, cfg)
            if day is None:
                last_day.append(inv)   # ultimo giorno utile, ora del giro passata
            else:
                planned.append((inv, day))

    items: List[dict] = []
    if sent:
        days, lines = set(), []
        for group in sent.values():
            moments = [event_time(r) for _i, r in group if event_time(r)]
            when = max(moments) if moments else None
            days.add(_local(when).date() if when else None)
            to = group[0][1].to_email
            lines.append(f"{_numbers(group)}: secondo avviso inviato {_when_text(when)}{' a ' + to if to else ''}")
        items.append(_item(
            KIND_FOLLOWUP, SENT, _counted(len(sent), days, "2° avviso", "secondi avvisi", today), lines,
            day=_latest(days), count=len(sent),
        ))
    if stuck:
        days, lines = set(), []
        for group in stuck.values():
            days.add(_latest({event_day(r) for _i, r in group}))
            newest = max((r for _i, r in group), key=lambda r: event_time(r) or datetime.min)
            lines.append(f"{_numbers(group)}: {_attempt_text(newest)}")
        items.append(_item(
            KIND_FOLLOWUP, STUCK, _counted(len(stuck), days, "2° avviso fermo", "secondi avvisi fermi", today),
            lines, ["Da gestire in Sistema → «Secondo avviso email» → Da gestire."],
            day=_latest(days), count=len(stuck),
        ))
    if last_day:
        items.append(_item(
            KIND_FOLLOWUP, STUCK, "2° avviso non ancora partito: ultimo giorno utile",
            (f"Fattura {invoice_label(i.invoice_number)}: oggi è l'ultimo giorno utile" for i in last_day),
            ["L'ora del giro in programma per oggi è passata. Può ancora partire oggi con «Esegui ora» in "
             "Sistema → «Secondo avviso email»; da domani non partirà più."],
            day=today, count=len(last_day),
        ))
    if orphans:
        days, lines = set(), []
        for group in orphans.values():
            days.add(_latest({event_day(r) for _i, r in group}))
            newest = max((r for _i, r in group), key=lambda r: event_time(r) or datetime.min)
            lines.append(f"{_numbers(group)}: {_attempt_text(newest)}")
        items.append(_item(
            KIND_FOLLOWUP, MISSING,
            _counted(len(orphans), days, "2° avviso dall'esito incerto", "secondi avvisi dall'esito incerto", today),
            lines,
            ["Riguarda una scadenza o un promemoria che la fattura non ha più: da Sistema non si ritenta. "
             "Si può verificare su Resend."],
            day=_latest(days), count=len(orphans),
        ))
    if planned or retrying:
        leaves = leave_days(planned + retrying, cfg, floors)
        first = min(leaves.values())
        lines = [f"Fattura {invoice_label(i.invoice_number)}: invio non riuscito, il giro ritenta il "
                 f"{leaves[i.id].strftime('%d/%m/%Y')}" for i, _own in retrying]
        lines += [f"Fattura {invoice_label(i.invoice_number)}: secondo avviso previsto il "
                  f"{leaves[i.id].strftime('%d/%m/%Y')}" for i, _own in planned]
        # Il primo giorno è di un nuovo tentativo se una delle fatture che
        # partono quel giorno è da ritentare.
        retry_first = any(leaves[i.id] == first for i, _own in retrying)
        label = f"2° avviso: nuovo tentativo {short_date(first, today)}" if retry_first else (
            f"2° avviso previsto {short_date(first, today)}")
        items.append(_item(
            KIND_FOLLOWUP, PLANNED, label, lines,
            ["È il prossimo giro in programma: parte solo se la fattura risulta ancora da saldare e le fatture "
             "sono state aggiornate dal sync. Un giro avviato a mano, o dopo un riavvio, può farlo partire prima."],
            day=first, count=len(planned) + len(retrying),
        ))
    for reason, invoices in _by_reason(now_skips).items():
        state = reason_state(reason)
        words = reason_text(reason)
        items.append(_item(
            KIND_FOLLOWUP, state, f"2° avviso {'sospeso' if state == PAUSED else 'non previsto'}: {words}",
            (f"Fattura {invoice_label(i.invoice_number)}: nei suoi giorni utili il giro la salta ({words})" for i in invoices),
            ["Vale finché vale questo motivo."],
            count=len(invoices),
        ))
    for reason, invoices in _by_reason(list(written.values())).items():
        words = reason_text(reason)
        items.append(_item(
            KIND_FOLLOWUP, fact_state(reason), f"2° avviso non partito: {words}",
            (f"Fattura {invoice_label(i.invoice_number)}: secondo avviso non inviato ({words})" for i in invoices),
            ["È l'ultimo motivo scritto dal giro nei giorni utili della fattura."],
            count=len(invoices),
        ))
    if expired:
        items.append(_item(
            KIND_FOLLOWUP, CLOSED, "2° avviso non previsto: giorni utili passati",
            (f"Fattura {invoice_label(i.invoice_number)}: i suoi giorni utili sono passati" for i in expired),
            [f"Il secondo avviso automatico parte solo nei giorni utili dopo la scadenza (dal {first_useful}° al "
             f"{last_useful}°, mai nel weekend)."],
            count=len(expired),
        ))
    return items


def customer_status(session: Session, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Per ogni cliente con almeno un'email automatica su una fattura ancora
    da saldare: le voci da mostrare sotto il suo nome, nell'ordine di lettura
    (promemoria, poi secondo avviso). {customers: {id: [voce]}, followup_mode}."""
    today = italian_today(now_utc)
    start = first_round_day(now_utc, cfg)
    now = followup._utc_now(now_utc)
    mode = followup.resolve_followup_mode(cfg)[0]
    merged = merge_map(session)
    # Prima il registro dei secondi avvisi: se non è leggibile, il rollback
    # avviene quando non c'è ancora nessuna riga caricata da rileggere.
    followups, readable = _safely(session, lambda: _open_rows(session, InvoiceFollowupEmail, merged), {})
    reminders = _open_rows(session, InvoiceReminderEmail, merged)
    per_customer: Dict[int, Dict[str, list]] = defaultdict(lambda: {"rem": [], "fol": []})
    for inv, rows in reminders.values():
        per_customer[inv.customer_id]["rem"].append((inv, rows))
    for inv, rows in followups.values():
        per_customer[inv.customer_id]["fol"].append((inv, rows))
    order = {key: n for n, key in enumerate(ITEM_ORDER)}
    customers: Dict[int, List[dict]] = {}
    for customer_id, found in per_customer.items():
        items = _reminder_items(found["rem"], today, now) if found["rem"] else []
        # Senza il registro dei secondi avvisi non si sa cosa è già partito:
        # nessuna voce sul secondo avviso, solo i promemoria (e la legenda lo dice).
        if readable:
            items += _followup_items(found["rem"], found["fol"], today, start, mode, cfg, now)
        if items:
            items.sort(key=lambda it: order.get((it["kind"], it["state"]), len(order)))
            customers[customer_id] = items
    return {
        "customers": customers, "followup_mode": mode, "followup_readable": readable,
        "today": today.isoformat(), "next_round": start.isoformat(),
        "read_at": _local(now).strftime("%H:%M"),
    }


# ── Registro «Email inviate» ───────────────────────────────────────

def _log_state(merged: Dict[int, int], reminded: set, kind: str, row, inv: Optional[Invoice], mode: str,
               today: date, start: date, cfg, now: datetime) -> Tuple[str, str]:
    """(stato, etichetta) di una riga nel registro: un fatto di quel giorno."""
    if row.status == STATUS_SENT:
        return SENT, "Inviata"
    if in_flight(row, now):
        return PLANNED, "Invio in corso"
    if not was_attempted(row):
        # Mai tentata: il giro l'ha saltata, col motivo scritto.
        return fact_state(row.skip_reason), f"Non partita: {reason_text(row.skip_reason)}"
    if inv is None or not followup.still_due(inv):
        return CLOSED, "Non conclusa: fattura non più da saldare"
    ours = row.due_date == inv.due_date and same_customer(merged, row.customer_id, inv.customer_id)
    if kind == KIND_REMINDER:
        if ours and row.status in STUCK_STATUSES and reminder_retriable(row, today):
            return STUCK, "Ferma: da gestire"
        # Solo l'ultimo errore è registrato: non si può dire «non partita».
        return MISSING, "Esito incerto: da verificare su Resend"
    if row.status == STATUS_SKIPPED:
        # Il motore non riscrive mai come «saltata» un invio incerto: questo
        # era un rifiuto certo, e a un giro successivo è stata saltata.
        return fact_state(row.skip_reason), f"Non riuscita, poi non partita: {reason_text(row.skip_reason)}"
    uncertain = followup._is_uncertain(row)
    # «Riprova» accetta solo la riga della scadenza attuale, del cliente a cui
    # la fattura appartiene ancora e col suo promemoria (retry_followup_row).
    if not ours or inv.id not in reminded:
        return (MISSING, "Esito incerto: da verificare su Resend") if uncertain else (
            CLOSED, "Non partita: invio non riuscito")
    if mode == MODE_REALE and not uncertain:
        day = planned_day(inv.due_date, start_after(row, today, start, cfg), cfg)
        if day is not None:
            reason = followup.exclusion(inv, inv.customer)
            if reason is None:
                return PLANNED, "Non riuscita: il giro la ritenta"
            state = reason_state(reason)
            return state, f"Non riuscita, ora {'sospeso' if state == PAUSED else 'non previsto'}: {reason_text(reason)}"
    return STUCK, "Ferma: da gestire"


def _copies(value) -> List[str]:
    """Le copie come elenco di indirizzi; qualunque altra forma (dato scritto
    a mano) non deve far cadere la pagina."""
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def _reminded_for(session: Session, merged: Dict[int, int], invoices: Dict[int, Invoice]) -> set:
    """reminded_ids per le fatture date, con UNA lettura del registro dei promemoria."""
    if not invoices:
        return set()
    rows = (
        session.query(InvoiceReminderEmail.invoice_id, InvoiceReminderEmail.customer_id, InvoiceReminderEmail.due_date)
        .filter(
            InvoiceReminderEmail.invoice_id.in_(list(invoices)),
            InvoiceReminderEmail.status == STATUS_SENT,
            InvoiceReminderEmail.mode == MODE_REALE,
        )
        .all()
    )
    return {
        invoice_id for invoice_id, customer_id, due in rows
        if due == invoices[invoice_id].due_date and same_customer(merged, customer_id, invoices[invoice_id].customer_id)
    }


def _entries(merged: Dict[int, int], reminded: set, kind: str, found: list, mode: str, today: date, start: date,
             cfg, now: datetime) -> List[dict]:
    """Le righe (riga, fattura, cliente) come voci del registro: un promemoria
    è una voce; le fatture di uno stesso secondo avviso sono una voce sola."""
    groups: Dict[tuple, list] = {}
    for row, inv, customer in found:
        state, label = _log_state(merged, reminded, kind, row, inv, mode, today, start, cfg, now)
        # Una email è di UN cliente: stessa chiave su clienti diversi = email diverse.
        key = (_email_key(row) if kind == KIND_FOLLOWUP else f"riga-{row.id}", row.customer_id, state, label)
        groups.setdefault(key, []).append((row, inv, customer))
    out: List[dict] = []
    for (_key, _customer_id, state, label), members in groups.items():
        members.sort(key=lambda m: (m[0].due_date or date.min, m[0].id))
        first_row, _first_inv, customer = members[0]
        moments = [event_time(r) for r, _i, _c in members if event_time(r)]
        when = _local(max(moments)) if moments else None
        errors = [r.error for r, _i, _c in members if r.error and r.status in STUCK_STATUSES]
        out.append({
            "key": f"{kind}-{first_row.id}",
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "at": when.isoformat() if when else None,
            "day": when.date().isoformat() if when else None,
            "time": when.strftime("%H:%M") if when else None,
            "customer_id": survivor_id(merged, customer.id) if customer is not None else None,
            "ragione_sociale": (customer.ragione_sociale if customer is not None else None)
            or next((i.customer_name_raw for _r, i, _c in members if i is not None and i.customer_name_raw), None),
            "agents": agents_of(customer),
            "invoices": [{
                "id": r.invoice_id,
                "number": invoice_label(i.invoice_number) if i is not None else None,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "amount_due": float(r.amount_due or 0),
            } for r, i, _c in members],
            "amount": round(sum(float(r.amount_due or 0) for r, _i, _c in members), 2),
            "to_email": first_row.to_email,
            "cc": _copies(first_row.cc_json),
            "state": state,
            "state_label": label,
            "detail": errors[0][:300] if errors else None,
            "row_ids": [r.id for r, _i, _c in members],
        })
    return out


def _log_query(session: Session, model):
    return (
        session.query(model, Invoice, Customer)
        .outerjoin(Invoice, Invoice.id == model.invoice_id)
        .outerjoin(Customer, Customer.id == model.customer_id)
        .options(defer(model.body_text), _with_customer())
        .filter(model.mode == MODE_REALE)
    )


def _sort_key(entry: dict):
    return (entry["at"] or "", entry["kind"], entry["key"])


def sent_log(session: Session, days: int = 14, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Promemoria e secondi avvisi REALI degli ultimi `days` giorni, insieme e
    dal più recente, raggruppati per giorno italiano; più TUTTE le email ferme
    che un operatore può riprendere (qualunque età: sono l'elenco «da gestire»).

    Un secondo avviso mai tentato e saltato dal giro NON è un fatto di un
    giorno (il giro lo rivaluta ogni mattina dei suoi giorni utili, senza
    datarlo): sta sotto il nome del cliente, non qui."""
    days = max(MIN_LOG_DAYS, min(int(days or MIN_LOG_DAYS), MAX_LOG_DAYS))
    today = italian_today(now_utc)
    start = first_round_day(now_utc, cfg)
    now = followup._utc_now(now_utc)
    first_day = today - timedelta(days=days - 1)
    since = (
        datetime.combine(first_day, time.min, tzinfo=ZoneInfo(config.TIMEZONE))
        .astimezone(timezone.utc).replace(tzinfo=None)
    )
    mode = followup.resolve_followup_mode(cfg)[0]
    merged = merge_map(session)

    def read(model, kind):
        # Lo stesso istante di event_time: l'invio per le inviate, l'ultimo
        # tentativo per le altre (un vecchio sent_at non deve spostarle).
        moment = case(
            (model.status == STATUS_SENT, func.coalesce(model.sent_at, model.attempted_at, model.created_at)),
            else_=func.coalesce(model.attempted_at, model.created_at),
        )
        recent = _log_query(session, model).filter(model.status.in_(LOG_STATUSES), moment >= since)
        if kind == KIND_FOLLOWUP:
            recent = recent.filter(not_(and_(model.status == STATUS_SKIPPED, model.attempted_at.is_(None))))
        recent = recent.order_by(model.id.asc()).all()
        halted = (
            _log_query(session, model)
            .filter(
                model.status.in_(STUCK_STATUSES),
                Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
                Invoice.amount_due > 0,
            )
            .order_by(model.id.asc())
            .all()
        )
        reminded = set()
        if kind == KIND_FOLLOWUP:
            reminded = _reminded_for(session, merged, {
                inv.id: inv for row, inv, _c in recent + halted if inv is not None and row.status in STUCK_STATUSES
            })
        return (
            [e for e in _entries(merged, reminded, kind, recent, mode, today, start, cfg, now)
             if e["day"] and e["day"] >= first_day.isoformat()],
            [e for e in _entries(merged, reminded, kind, halted, mode, today, start, cfg, now) if e["state"] == STUCK],
        )

    entries: List[dict] = []
    stuck: List[dict] = []
    # Prima il registro dei secondi avvisi (vedi customer_status).
    (found, halted), readable = _safely(session, lambda: read(InvoiceFollowupEmail, KIND_FOLLOWUP), ([], []))
    entries.extend(found)
    stuck.extend(halted)
    found, halted = read(InvoiceReminderEmail, KIND_REMINDER)
    entries.extend(found)
    stuck.extend(halted)
    entries.sort(key=_sort_key, reverse=True)
    stuck.sort(key=_sort_key, reverse=True)

    by_day: Dict[str, List[dict]] = {}
    for entry in entries:
        by_day.setdefault(entry["day"], []).append(entry)
    week_start = (today - timedelta(days=6)).isoformat()
    out_days = []
    for day_key in sorted(by_day, reverse=True):
        day = date.fromisoformat(day_key)
        items = by_day[day_key]
        out_days.append({
            "date": day_key,
            "label": f"{WEEKDAYS_IT[day.weekday()].capitalize()} {day.strftime('%d/%m/%Y')}",
            "sent": sum(1 for e in items if e["state"] == SENT),
            "stuck": sum(1 for e in items if e["state"] == STUCK),
            "missing": sum(1 for e in items if e["state"] == MISSING),
            "items": items,
        })
    return {
        "days": out_days,
        "stuck": stuck,
        "totals": {
            "today_sent": sum(1 for e in entries if e["state"] == SENT and e["day"] == today.isoformat()),
            "week_sent": sum(1 for e in entries if e["state"] == SENT and e["day"] >= week_start),
            "week_missing": sum(1 for e in entries if e["state"] == MISSING and e["day"] >= week_start),
            "stuck": len(stuck),
        },
        "range": {"from": first_day.isoformat(), "to": today.isoformat(), "days": days, "max_days": MAX_LOG_DAYS},
        "followup_mode": mode,
        "followup_readable": readable,
        "today": today.isoformat(),
        "read_at": _local(now).strftime("%H:%M"),
    }
