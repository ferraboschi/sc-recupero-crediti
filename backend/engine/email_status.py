"""Stato delle email automatiche accanto al nome del cliente e registro unico
«Email inviate» (richiesta owner 24–26/11/2026: «deve essere facile capire lo
stato della segnalazione, per evitare la over segnalazione ma anche di perdere
segnalazioni per strada»; «a Luigi serve sapere quali mail sono partite e
quando»).

SOLA LETTURA dei due registri (promemoria pre-scadenza e secondo avviso): qui
non si invia, non si scrive e non si decide nulla. Si mostrano solo fatti —
ciò che il registro dice degli invii REALI — con una sola previsione: il
giorno in cui il giro del mattino manderà il secondo avviso, calcolato con le
regole del giro (backend/engine/followup_email.py) e mostrato solo quando il
secondo avviso è acceso in modalità reale.

Regole di lettura:
- contano solo le righe in modalità REALE: una prova o un test non sono
  arrivati al cliente;
- una riga riguarda un cliente solo se la fattura è ancora sua (una scheda
  unita a un'altra è lo stesso cliente): l'email mandata a un altro non
  compare sotto il suo nome;
- accanto al nome si parla delle sole fatture ANCORA DA SALDARE; il registro
  «Email inviate» elenca invece tutto ciò che è successo, giorno per giorno;
- il motivo di un mancato invio vale per la scadenza che la fattura ha
  ADESSO; se oggi quel motivo non c'è più lo si dice («allora in attesa»):
  uno stato passato non va letto come uno stato presente;
- «previsto» è una promessa: si fa solo per un giro che deve ANCORA partire
  (dopo l'ora del giro di oggi si parla di domani) e mai senza aver letto il
  registro dei secondi avvisi;
- «ferma, da gestire» si dice solo di ciò che un operatore può davvero
  riprendere da Sistema; di un invio che potrebbe essere arrivato non si dice
  mai «non partito»: si dice «esito incerto»;
- i nomi sono quelli delle email («Promemoria», «2° avviso»), mai «prima /
  seconda segnalazione»: in Attività I e II contatto sono già le segnalazioni
  fatte a mano dall'operatore.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import case, func
from sqlalchemy.orm import Session, defer, joinedload

from backend.config import config
from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail
from backend.engine import followup_email as followup
from backend.engine.agents import agents_of
from backend.engine.format import italian_today
from backend.engine.reminder_email import (
    MODE_REALE, NOT_REMINDABLE_STATUSES, STATUS_FAILED, STATUS_SENDING, STATUS_SENT, STATUS_SKIPPED,
    WEEKDAYS_IT, invoice_label, skip_reason as reminder_skip_reason,
)

logger = logging.getLogger(__name__)

KIND_REMINDER = "promemoria"
KIND_FOLLOWUP = followup.KIND_KEY
KIND_LABELS = {KIND_REMINDER: "Promemoria", KIND_FOLLOWUP: "2° avviso"}

# Come si presenta un'email accanto al nome (un colore = un significato).
SENT = "sent"          # arrivata al cliente
PLANNED = "planned"    # il giro la manderà (o la ritenterà) da solo
STUCK = "stuck"        # ferma: un operatore può riprenderla da Sistema («Riprova»)
MISSING = "missing"    # non partita, o dall'esito incerto: il cliente potrebbe non averla
PAUSED = "paused"      # non partita per una scelta o uno stato della pratica che vale ancora
CLOSED = "closed"      # (solo registro) non conclusa, fattura non più da saldare

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
# Motivi che NON chiedono di sistemare nulla: è una scelta dell'operatore o
# uno stato della pratica. Ogni altro motivo (anche uno nuovo, non ancora in
# elenco) è «da sistemare»: nel dubbio si segnala.
PAUSE_REASONS = frozenset((
    "cliente_unito", "cliente_escluso", "avvocato", "in_incasso", "forse_pagata", "in_attesa", "promessa",
))

MIN_LOG_DAYS = 7
MAX_LOG_DAYS = 90
MAX_TITLE_LINES = 6

ITEM_ORDER = (
    (KIND_REMINDER, SENT), (KIND_REMINDER, STUCK), (KIND_REMINDER, MISSING), (KIND_REMINDER, PAUSED),
    (KIND_FOLLOWUP, SENT), (KIND_FOLLOWUP, STUCK), (KIND_FOLLOWUP, PLANNED), (KIND_FOLLOWUP, MISSING),
    (KIND_FOLLOWUP, PAUSED),
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
    return PAUSED if code in PAUSE_REASONS else MISSING


def first_round_day(now_utc: Optional[datetime] = None, cfg=None) -> date:
    """Il primo giorno il cui giro del mattino deve ANCORA partire: oggi, se è
    un giorno lavorativo e l'ora del giro (REMINDER_EMAIL_HOUR:MINUTE, ora
    italiana) non è passata; altrimenti il prossimo giorno lavorativo. Dopo
    quell'ora per oggi non c'è più nessun giro in programma (uno a mano, o il
    recupero dopo un riavvio, possono ancora partire: ma non sono una
    promessa che si possa fare)."""
    cfg = cfg or config
    local = _local(followup._utc_now(now_utc))
    day = local.date()
    at = (int(getattr(cfg, "REMINDER_EMAIL_HOUR", 9)), int(getattr(cfg, "REMINDER_EMAIL_MINUTE", 15)))
    if (local.hour, local.minute) >= at:
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

def _open_rows(session: Session, model, merged: Dict[int, int]) -> Dict[int, Tuple[Invoice, list]]:
    """Le righe REALI di fatture ancora da saldare, per fattura (dalla
    scadenza più vecchia): {id fattura: (fattura, righe)}. Solo righe del
    cliente a cui la fattura appartiene ora."""
    found = (
        session.query(model, Invoice)
        .join(Invoice, Invoice.id == model.invoice_id)
        # Il testo dell'email non serve qui: non va letto a ogni apertura di pagina.
        .options(joinedload(Invoice.customer), defer(model.body_text))
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
    """La riga della scadenza che la fattura ha ADESSO (ce n'è al più una).
    Stato e motivo si leggono lì: se la scadenza è cambiata, il giro la
    rivaluterà alla nuova data."""
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


def _item(kind: str, state: str, label: str, lines: Iterable[str], day: Optional[date] = None, count: int = 1) -> dict:
    lines = list(lines)
    if len(lines) > MAX_TITLE_LINES:
        lines = lines[:MAX_TITLE_LINES] + [f"… e altre {len(lines) - MAX_TITLE_LINES}"]
    return {
        "kind": kind, "state": state, "label": label, "title": "\n".join(lines),
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


def _skip_groups(found: List[Tuple[Invoice, Optional[str], bool]]) -> Dict[Tuple[Optional[str], bool], List[Invoice]]:
    """Le fatture non partite, per (motivo, vale ancora oggi?)."""
    grouped: Dict[Tuple[Optional[str], bool], List[Invoice]] = {}
    for inv, reason, holds in found:
        grouped.setdefault((reason or None, bool(holds)), []).append(inv)
    return grouped


# ── Promemoria accanto al nome ─────────────────────────────────────

def _reminder_items(entries: List[Tuple[Invoice, list]], today: date) -> List[dict]:
    """Promemoria pre-scadenza delle fatture ancora da saldare di un cliente."""
    sent: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    stuck: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    uncertain: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    skipped: List[Tuple[Invoice, Optional[str], bool]] = []
    for inv, rows in entries:
        sent.extend((inv, r) for r in rows if r.status == STATUS_SENT)
        current = _current(rows, inv)
        if current is None:
            continue
        if current.status in STUCK_STATUSES:
            (stuck if reminder_retriable(current, today) else uncertain).append((inv, current))
        elif current.status == STATUS_SKIPPED:
            reason = current.skip_reason or None
            skipped.append((inv, reason, reminder_skip_reason(inv, inv.customer) == reason))
    items: List[dict] = []
    if sent:
        days = {event_day(r) for _i, r in sent}
        items.append(_item(
            KIND_REMINDER, SENT, _counted(len(sent), days, "Promemoria", "promemoria", today),
            (f"Fattura {invoice_label(i.invoice_number)}: promemoria inviato {_when_text(event_time(r))}"
             f"{' a ' + r.to_email if r.to_email else ''}" for i, r in sent),
            _latest(days), len(sent),
        ))
    if stuck:
        days = {event_day(r) for _i, r in stuck}
        items.append(_item(
            KIND_REMINDER, STUCK, _counted(len(stuck), days, "Promemoria fermo", "promemoria fermi", today),
            [f"Fattura {invoice_label(i.invoice_number)}: ultimo tentativo {_when_text(event_time(r))}, non concluso"
             for i, r in stuck]
            + ["Da gestire in Sistema → «Promemoria email». Prima di ritentare, verificare su Resend che un "
               "tentativo precedente non sia già arrivato."],
            _latest(days), len(stuck),
        ))
    if uncertain:
        # Il registro dei promemoria ricorda solo l'ULTIMO errore: dopo un
        # «Riprova» un rifiuto può nascondere un primo tentativo arrivato.
        # Quindi mai «non partito»: non lo si sa.
        days = {event_day(r) for _i, r in uncertain}
        items.append(_item(
            KIND_REMINDER, MISSING,
            _counted(len(uncertain), days, "Promemoria dall'esito incerto", "promemoria dall'esito incerto", today),
            [f"Fattura {invoice_label(i.invoice_number)}: ultimo tentativo {_when_text(event_time(r))}, non concluso"
             f"{' (' + r.error[:120] + ')' if r.error else ''}" for i, r in uncertain]
            + ["Non si sa se il cliente l'ha ricevuto: si può verificare su Resend. La scadenza è passata, non si "
               "può più ritentare; il secondo avviso automatico non partirà."],
            _latest(days), len(uncertain),
        ))
    for (reason, holds), invoices in _skip_groups(skipped).items():
        n = len(invoices)
        words = reason_text(reason) if holds else f"allora {reason_text(reason)}"
        items.append(_item(
            KIND_REMINDER, reason_state(reason) if holds else MISSING,
            f"Promemoria non partito: {words}" if n == 1 else f"{n} promemoria non partiti: {words}",
            [f"Fattura {invoice_label(i.invoice_number)}: promemoria non inviato ({reason_text(reason)})" for i in invoices]
            + ([] if holds else ["Il motivo valeva al momento del giro; oggi non più."])
            + ["Senza promemoria il secondo avviso automatico non parte."],
            None, n,
        ))
    return items


# ── Secondo avviso accanto al nome ─────────────────────────────────

def _attempt_text(row) -> str:
    """Un tentativo fermo del secondo avviso: è certo che non sia partito, o
    potrebbe essere arrivato? (il motore tiene distinta la cosa)"""
    kind = "dall'esito incerto, potrebbe essere arrivato" if followup._is_uncertain(row) else "non riuscito"
    return f"invio {kind} ({_when_text(event_time(row))})"


def _followup_items(reminders: List[Tuple[Invoice, list]], followups: List[Tuple[Invoice, list]], today: date,
                    start: date, mode: str, cfg) -> List[dict]:
    """Secondo avviso delle fatture ancora da saldare di un cliente: ciò che
    è partito, ciò che è fermo e — solo da acceso in reale — ciò che il giro
    manderà. `start`: il primo giorno il cui giro deve ancora partire."""
    live = mode == MODE_REALE
    # Stessa regola del giro (reminder_sent): promemoria reale inviato per
    # QUESTA scadenza a questo cliente. Senza, il giro non guarda la fattura.
    reminded = {
        inv.id for inv, rows in reminders
        if any(r.status == STATUS_SENT and r.due_date == inv.due_date for r in rows)
    }
    sent: Dict[str, list] = {}
    stuck: Dict[str, list] = {}
    orphans: List[Tuple[Invoice, InvoiceFollowupEmail]] = []
    retrying: List[Tuple[Invoice, date]] = []
    planned: List[Tuple[Invoice, date]] = []
    pending_skips: List[Tuple[Invoice, Optional[str], bool]] = []
    deferred: Dict[int, Tuple[Invoice, Optional[str]]] = {}  # saltate per la scadenza attuale
    blocked_ids = set()  # fatture la cui scadenza attuale ha già una riga che parla
    for inv, rows in followups:
        current = _current(rows, inv)
        for r in rows:
            if r.status == STATUS_SENT:
                sent.setdefault(_email_key(r), []).append((inv, r))
            elif r.status in STUCK_STATUSES and r is not current and followup._is_uncertain(r):
                # Tentativo per una scadenza poi cambiata: «Riprova» lo rifiuta
                # e nessun giro lo riprende. Se era incerto, resta da dire.
                orphans.append((inv, r))
        if current is None:
            continue
        if current.status == STATUS_SENT:
            blocked_ids.add(inv.id)
        elif current.status in STUCK_STATUSES:
            blocked_ids.add(inv.id)
            # Il giro riprende da solo ciò che CERTAMENTE non è partito, se ha
            # ancora un giro davanti nei giorni utili; il resto è dell'operatore.
            day = None
            if live and inv.id in reminded and not followup._is_uncertain(current):
                day = planned_day(inv.due_date, start, cfg)
            if day is None:
                stuck.setdefault(_email_key(current), []).append((inv, current))
            else:
                reason = followup.exclusion(inv, inv.customer)
                if reason:
                    pending_skips.append((inv, reason, True))
                else:
                    retrying.append((inv, day))
        elif current.status == STATUS_SKIPPED:
            deferred[inv.id] = (inv, current.skip_reason or None)

    if live:
        for inv, _rows in reminders:
            if inv.id in blocked_ids or inv.id not in reminded or inv.due_date is None or inv.due_date >= today:
                continue
            day = planned_day(inv.due_date, start, cfg)
            if day is None:
                continue
            # Dentro i giorni utili il giro rivaluta ogni mattina anche le
            # saltate: conta lo stato di ADESSO, non il motivo scritto ieri.
            deferred.pop(inv.id, None)
            reason = followup.exclusion(inv, inv.customer)
            if reason:
                pending_skips.append((inv, reason, True))
            else:
                planned.append((inv, day))
    # Ciò che il giro non rivaluterà più: resta il motivo scritto, detto al
    # presente solo se oggi vale ancora.
    final_skips: List[Tuple[Invoice, Optional[str], bool]] = []
    for inv, reason in deferred.values():
        if reason == "senza_promemoria":
            holds = inv.id not in reminded
        else:
            holds = followup.exclusion(inv, inv.customer) == reason
        final_skips.append((inv, reason, holds))

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
            _latest(days), len(sent),
        ))
    if stuck:
        days, lines = set(), []
        for group in stuck.values():
            days.add(_latest({event_day(r) for _i, r in group}))
            newest = max((r for _i, r in group), key=lambda r: event_time(r) or datetime.min)
            lines.append(f"{_numbers(group)}: {_attempt_text(newest)}")
        items.append(_item(
            KIND_FOLLOWUP, STUCK, _counted(len(stuck), days, "2° avviso fermo", "secondi avvisi fermi", today),
            lines + ["Da gestire in Sistema → «Secondo avviso email» → Da gestire."],
            _latest(days), len(stuck),
        ))
    if orphans:
        days = {event_day(r) for _i, r in orphans}
        items.append(_item(
            KIND_FOLLOWUP, MISSING,
            _counted(len(orphans), days, "2° avviso dall'esito incerto", "secondi avvisi dall'esito incerto", today),
            [f"Fattura {invoice_label(i.invoice_number)}: {_attempt_text(r)}" for i, r in orphans]
            + ["Riguardava una scadenza poi cambiata: non si può più ritentare. Si può verificare su Resend."],
            _latest(days), len(orphans),
        ))
    if planned or retrying:
        first = min(day for _i, day in planned + retrying)
        # Una email per cliente: con la prima partono anche le fatture che
        # quel giorno sono a un giorno dalla soglia (scadenza prossima).
        merged_until = first - timedelta(days=followup.followup_days(cfg)) + timedelta(days=followup.NEAR_DUE_DAYS)

        def leaves(inv: Invoice, own: date) -> date:
            return first if inv.due_date <= merged_until else own

        retry_first = any(leaves(inv, own) == first for inv, own in retrying)
        lines = [f"Fattura {invoice_label(i.invoice_number)}: invio non riuscito, il giro ritenta il "
                 f"{leaves(i, own).strftime('%d/%m/%Y')}" for i, own in retrying]
        lines += [f"Fattura {invoice_label(i.invoice_number)}: secondo avviso previsto il "
                  f"{leaves(i, own).strftime('%d/%m/%Y')}" for i, own in planned]
        lines.append("Parte nel giro del mattino, solo se la fattura risulta ancora da saldare e le fatture "
                     "sono state aggiornate dal sync.")
        label = f"2° avviso: nuovo tentativo {short_date(first, today)}" if retry_first else (
            f"2° avviso previsto {short_date(first, today)}")
        items.append(_item(KIND_FOLLOWUP, PLANNED, label, lines, first, len(planned) + len(retrying)))
    for (reason, _holds), invoices in _skip_groups(pending_skips).items():
        state = reason_state(reason)
        label = f"2° avviso {'sospeso' if state == PAUSED else 'non previsto'}: {reason_text(reason)}"
        items.append(_item(
            KIND_FOLLOWUP, state, label,
            (f"Fattura {invoice_label(i.invoice_number)}: il secondo avviso non parte finché vale questo motivo "
             f"({reason_text(reason)})" for i in invoices),
            None, len(invoices),
        ))
    for (reason, holds), invoices in _skip_groups(final_skips).items():
        words = reason_text(reason) if holds else f"allora {reason_text(reason)}"
        items.append(_item(
            KIND_FOLLOWUP, reason_state(reason) if holds else MISSING, f"2° avviso non partito: {words}",
            [f"Fattura {invoice_label(i.invoice_number)}: secondo avviso non inviato ({reason_text(reason)})" for i in invoices]
            + ([] if holds else ["Il motivo valeva nei giorni utili; oggi non più."])
            + (["I giorni utili per l'invio automatico sono passati."] if live else []),
            None, len(invoices),
        ))
    return items


def customer_status(session: Session, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Per ogni cliente con almeno un'email automatica su una fattura ancora
    da saldare: le voci da mostrare sotto il suo nome, nell'ordine di lettura
    (promemoria, poi secondo avviso). {customers: {id: [voce]}, followup_mode}."""
    today = italian_today(now_utc)
    start = first_round_day(now_utc, cfg)
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
        items = _reminder_items(found["rem"], today) if found["rem"] else []
        # Senza il registro dei secondi avvisi non si sa cosa è già partito:
        # niente previsioni, solo i promemoria (e la legenda lo dice).
        if readable:
            items += _followup_items(found["rem"], found["fol"], today, start, mode, cfg)
        if items:
            items.sort(key=lambda it: order.get((it["kind"], it["state"]), len(order)))
            customers[customer_id] = items
    return {
        "customers": customers, "followup_mode": mode, "followup_readable": readable,
        "today": today.isoformat(), "next_round": start.isoformat(),
    }


# ── Registro «Email inviate» ───────────────────────────────────────

def _log_state(session: Session, merged: Dict[int, int], kind: str, row, inv: Optional[Invoice], mode: str,
               today: date, start: date, cfg) -> Tuple[str, str]:
    """(stato, etichetta) di una riga nel registro."""
    if row.status == STATUS_SENT:
        return SENT, "Inviata"
    if row.status == STATUS_SKIPPED:
        state = reason_state(row.skip_reason)
        return state, f"{'Sospesa' if state == PAUSED else 'Non partita'}: {reason_text(row.skip_reason)}"
    if inv is None or not followup.still_due(inv):
        return CLOSED, "Non conclusa: fattura non più da saldare"
    if kind == KIND_REMINDER:
        if reminder_retriable(row, today):
            return STUCK, "Ferma: da gestire"
        # Solo l'ultimo errore è registrato: non si può dire «non partita».
        return MISSING, "Esito incerto: non più ritentabile"
    uncertain = followup._is_uncertain(row)
    # «Riprova» accetta solo la riga della scadenza attuale, del cliente a cui
    # la fattura appartiene ancora (followup_email.retry_followup_row).
    if row.due_date != inv.due_date or not same_customer(merged, row.customer_id, inv.customer_id):
        return (MISSING, "Esito incerto: non più ritentabile") if uncertain else (MISSING, "Non partita: invio non riuscito")
    if (mode == MODE_REALE and not uncertain and planned_day(inv.due_date, start, cfg) is not None
            and followup.reminder_sent(session, inv) is not None):
        reason = followup.exclusion(inv, inv.customer)
        if reason is None:
            return PLANNED, "Non riuscita: il giro ritenta da solo"
        state = reason_state(reason)
        return state, f"Non riuscita, {'ora sospesa' if state == PAUSED else 'non ripartirà'}: {reason_text(reason)}"
    return STUCK, "Ferma: da gestire"


def _copies(value) -> List[str]:
    """Le copie come elenco di indirizzi; qualunque altra forma (dato scritto
    a mano) non deve far cadere la pagina."""
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def _entries(session: Session, merged: Dict[int, int], kind: str, found: list, mode: str, today: date, start: date,
             cfg) -> List[dict]:
    """Le righe (riga, fattura, cliente) come voci del registro: un promemoria
    è una voce; le fatture di uno stesso secondo avviso sono una voce sola."""
    groups: Dict[tuple, list] = {}
    for row, inv, customer in found:
        state, label = _log_state(session, merged, kind, row, inv, mode, today, start, cfg)
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
        .options(defer(model.body_text))
        .filter(model.mode == MODE_REALE)
    )


def _sort_key(entry: dict):
    return (entry["at"] or "", entry["kind"], entry["key"])


def sent_log(session: Session, days: int = 14, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Promemoria e secondi avvisi REALI degli ultimi `days` giorni, insieme e
    dal più recente, raggruppati per giorno italiano; più TUTTE le email ferme
    che un operatore può riprendere (qualunque età: sono l'elenco «da gestire»)."""
    days = max(MIN_LOG_DAYS, min(int(days or MIN_LOG_DAYS), MAX_LOG_DAYS))
    today = italian_today(now_utc)
    start = first_round_day(now_utc, cfg)
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
        recent = (
            _log_query(session, model)
            .filter(model.status.in_(LOG_STATUSES), moment >= since)
            .order_by(model.id.asc())
            .all()
        )
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
        return (
            [e for e in _entries(session, merged, kind, recent, mode, today, start, cfg)
             if e["day"] and e["day"] >= first_day.isoformat()],
            [e for e in _entries(session, merged, kind, halted, mode, today, start, cfg) if e["state"] == STUCK],
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
            "paused": sum(1 for e in items if e["state"] == PAUSED),
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
    }
