"""Stato delle email automatiche accanto al nome del cliente e registro unico
«Email inviate» (richiesta owner 24–26/11/2026: «deve essere facile capire lo
stato della segnalazione, per evitare la over segnalazione ma anche di perdere
segnalazioni per strada»; «a Luigi serve sapere quali mail sono partite e
quando»).

SOLA LETTURA dei due registri (promemoria pre-scadenza e secondo avviso): qui
non si invia, non si scrive e non si decide nulla. Si mostrano solo fatti —
ciò che il registro dice degli invii REALI — con una sola previsione: il
giorno in cui il giro manderà il secondo avviso, calcolato con le regole del
giro (backend/engine/followup_email.py) e mostrato solo quando il secondo
avviso è acceso in modalità reale.

Regole di lettura:
- contano solo le righe in modalità REALE: una prova o un test non sono
  arrivati al cliente;
- una riga riguarda un cliente solo se la fattura è ancora sua (una scheda
  unita a un'altra è lo stesso cliente): l'email mandata a un altro non
  compare sotto il suo nome;
- accanto al nome si parla delle sole fatture ANCORA DA SALDARE; il registro
  «Email inviate» elenca invece tutto ciò che è successo, giorno per giorno;
- i nomi sono quelli delle email («Promemoria», «2° avviso»), mai «prima /
  seconda segnalazione»: in Attività I e II contatto sono già le segnalazioni
  fatte a mano dall'operatore.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from backend.config import config
from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail
from backend.engine import followup_email as followup
from backend.engine.agents import agents_of
from backend.engine.format import italian_today
from backend.engine.reminder_email import (
    AUTO_RETRY_KINDS, ERROR_OTHER, MODE_REALE, NOT_REMINDABLE_STATUSES, STATUS_FAILED, STATUS_SENDING, STATUS_SENT,
    STATUS_SKIPPED, WEEKDAYS_IT, invoice_label,
)

logger = logging.getLogger(__name__)

KIND_REMINDER = "promemoria"
KIND_FOLLOWUP = followup.KIND_KEY
KIND_LABELS = {KIND_REMINDER: "Promemoria", KIND_FOLLOWUP: "2° avviso"}

# Come si presenta un'email accanto al nome (un colore = un significato).
SENT = "sent"          # arrivata al cliente
PLANNED = "planned"    # il giro la manderà (o la ritenterà) da solo
STUCK = "stuck"        # ferma: serve un operatore (Sistema → «Riprova»)
MISSING = "missing"    # non partita per un dato da sistemare
PAUSED = "paused"      # non partita per una scelta o uno stato della pratica
CLOSED = "closed"      # (solo registro) non riuscita, fattura non più da saldare

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


def planned_day(due: Optional[date], today: date, cfg=None) -> Optional[date]:
    """Il primo giorno, da oggi in poi, in cui il giro del secondo avviso
    prende una fattura con questa scadenza: N giorni dopo la scadenza, mai nel
    weekend, entro i giorni di recupero. None se quella finestra è passata.
    È la stessa finestra di followup_window letta dalla parte della fattura."""
    if due is None:
        return None
    first = due + timedelta(days=followup.followup_days(cfg))
    last = first + timedelta(days=followup.followup_lookback(cfg))
    day = max(today, first)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day if day <= last else None


# ── Lettura dei registri ───────────────────────────────────────────

def _open_rows(session: Session, model) -> Dict[int, Tuple[Invoice, list]]:
    """Le righe REALI di fatture ancora da saldare, per fattura (dalla
    scadenza più vecchia): {id fattura: (fattura, righe)}. Solo righe del
    cliente a cui la fattura appartiene ora."""
    found = (
        session.query(model, Invoice)
        .join(Invoice, Invoice.id == model.invoice_id)
        .options(joinedload(Invoice.customer))
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
        if not followup._is_same_customer(session, row.customer_id, inv.customer_id):
            continue
        grouped.setdefault(inv.id, (inv, []))[1].append(row)
    return grouped


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


def _current(rows: list, inv: Invoice):
    """La riga della scadenza che la fattura ha ADESSO (ce n'è al più una).
    Il motivo di un mancato invio vale per quella: se la scadenza è cambiata,
    il giro la rivaluterà alla nuova data."""
    return next((r for r in rows if r.due_date == inv.due_date), None)


RETRIABLE = "retriable"   # promemoria fermo, scadenza non ancora passata: «Riprova» in Sistema
LOST = "lost"             # certamente non partito, e la scadenza è passata
UNCERTAIN = "uncertain"   # forse arrivato, forse no, e la scadenza è passata


def reminder_trouble(row, today: date) -> str:
    """Che ne è di un promemoria non riuscito (fallito o interrotto). «Riprova»
    funziona solo finché la scadenza non è passata (reminder_email.retry_row):
    dopo non è più un promemoria, e nessuno lo manderà. Allora conta se
    l'invio è CERTAMENTE non partito (PDF non generato, rifiuto di Resend) o
    se potrebbe essere arrivato (timeout, errore del provider, interruzione)."""
    if row.due_date is not None and row.due_date >= today:
        return RETRIABLE
    certain = row.status == STATUS_FAILED and (row.error_kind or ERROR_OTHER) in AUTO_RETRY_KINDS
    return LOST if certain else UNCERTAIN


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


def _by_reason(pairs: List[Tuple[Invoice, str]]) -> Dict[str, List[Invoice]]:
    grouped: Dict[str, List[Invoice]] = {}
    for inv, reason in pairs:
        grouped.setdefault(reason, []).append(inv)
    return grouped


def _reminder_items(entries: List[Tuple[Invoice, list]], today: date) -> List[dict]:
    """Promemoria pre-scadenza delle fatture ancora da saldare di un cliente."""
    sent: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    stuck: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    lost: List[Invoice] = []
    uncertain: List[Tuple[Invoice, InvoiceReminderEmail]] = []
    skipped: List[Tuple[Invoice, str]] = []
    for inv, rows in entries:
        sent.extend((inv, r) for r in rows if r.status == STATUS_SENT)
        current = _current(rows, inv)
        if current is None:
            continue
        if current.status in STUCK_STATUSES:
            fate = reminder_trouble(current, today)
            if fate == RETRIABLE:
                stuck.append((inv, current))
            elif fate == LOST:
                lost.append(inv)
            else:
                uncertain.append((inv, current))
        elif current.status == STATUS_SKIPPED:
            skipped.append((inv, current.skip_reason))
    items: List[dict] = []
    if sent:
        days = {event_day(r) for _i, r in sent}
        known = [d for d in days if d is not None]
        items.append(_item(
            KIND_REMINDER, SENT, _counted(len(sent), days, "Promemoria", "promemoria", today),
            (f"Fattura {invoice_label(i.invoice_number)}: promemoria inviato {_when_text(event_time(r))}"
             f"{' a ' + r.to_email if r.to_email else ''}" for i, r in sent),
            max(known) if known else None, len(sent),
        ))
    if stuck:
        days = {event_day(r) for _i, r in stuck}
        known = [d for d in days if d is not None]
        items.append(_item(
            KIND_REMINDER, STUCK, _counted(len(stuck), days, "Promemoria fermo", "promemoria fermi", today),
            [f"Fattura {invoice_label(i.invoice_number)}: invio non riuscito {_when_text(event_time(r))}" for i, r in stuck]
            + ["Si può ritentare da Sistema → «Promemoria email», finché la scadenza non è passata."],
            max(known) if known else None, len(stuck),
        ))
    if uncertain:
        days = {event_day(r) for _i, r in uncertain}
        known = [d for d in days if d is not None]
        items.append(_item(
            KIND_REMINDER, MISSING,
            _counted(len(uncertain), days, "Promemoria dall'esito incerto", "promemoria dall'esito incerto", today),
            [f"Fattura {invoice_label(i.invoice_number)}: invio interrotto {_when_text(event_time(r))}, non si sa se è arrivato"
             for i, r in uncertain]
            + ["La scadenza è passata: non si può più ritentare. Il secondo avviso automatico non partirà."],
            max(known) if known else None, len(uncertain),
        ))
    if lost:
        n = len(lost)
        items.append(_item(
            KIND_REMINDER, MISSING,
            "Promemoria non partito: invio non riuscito" if n == 1 else f"{n} promemoria non partiti: invio non riuscito",
            [f"Fattura {invoice_label(i.invoice_number)}: promemoria non inviato (invio non riuscito)" for i in lost]
            + ["La scadenza è passata: non si può più ritentare. Il secondo avviso automatico non partirà."],
            None, n,
        ))
    for reason, invoices in _by_reason(skipped).items():
        state = reason_state(reason)
        n = len(invoices)
        if state == PAUSED:
            label = f"Promemoria sospeso: {reason_text(reason)}" if n == 1 else f"{n} promemoria sospesi: {reason_text(reason)}"
        else:
            label = (f"Promemoria non partito: {reason_text(reason)}" if n == 1
                     else f"{n} promemoria non partiti: {reason_text(reason)}")
        items.append(_item(
            KIND_REMINDER, state, label,
            (f"Fattura {invoice_label(i.invoice_number)}: promemoria non inviato ({reason_text(reason)})" for i in invoices),
            None, n,
        ))
    return items


def _email_key(row) -> str:
    return row.group_key or f"riga-{row.id}"


def _followup_items(reminders: List[Tuple[Invoice, list]], followups: List[Tuple[Invoice, list]], today: date,
                    mode: str, now_utc: Optional[datetime], cfg) -> List[dict]:
    """Secondo avviso delle fatture ancora da saldare di un cliente: ciò che
    è partito, ciò che è fermo e — solo da acceso in reale — ciò che il giro
    manderà."""
    live = mode == MODE_REALE
    sent: Dict[str, List[Tuple[Invoice, InvoiceFollowupEmail]]] = {}
    stuck: Dict[str, List[Tuple[Invoice, InvoiceFollowupEmail]]] = {}
    retrying: List[Invoice] = []
    final_skips: List[Tuple[Invoice, str]] = []
    deferred: Dict[int, Tuple[Invoice, str]] = {}  # saltate per la scadenza attuale
    blocked_ids = set()  # fatture la cui scadenza attuale ha già una riga che parla
    for inv, rows in followups:
        current = _current(rows, inv)
        for r in rows:
            if r.status == STATUS_SENT:
                sent.setdefault(_email_key(r), []).append((inv, r))
            elif r.status in STUCK_STATUSES:
                # Il giro riprende solo la riga della scadenza attuale; ogni
                # altra riga ferma resta dell'operatore (è in «Da gestire»).
                if live and r is current and followup.will_auto_retry(r, inv, MODE_REALE, now_utc, cfg):
                    retrying.append(inv)
                else:
                    stuck.setdefault(_email_key(r), []).append((inv, r))
        if current is None:
            continue
        if current.status == STATUS_SENT or current.status in STUCK_STATUSES:
            blocked_ids.add(inv.id)
        elif current.status == STATUS_SKIPPED:
            # Dentro la finestra il giro la rivaluta ogni mattina: se è tra
            # quelle che rivaluterà (sotto) conta lo stato di ADESSO,
            # altrimenti resta il motivo scritto nel registro.
            deferred[inv.id] = (inv, current.skip_reason)

    planned: List[Tuple[Invoice, date]] = []
    pending_skips: List[Tuple[Invoice, str]] = []
    if live:
        for inv, rows in reminders:
            if inv.id in blocked_ids or inv.due_date is None or inv.due_date >= today:
                continue
            # Stessa regola del giro (reminder_sent): promemoria reale inviato
            # per QUESTA scadenza a questo cliente.
            if not any(r.status == STATUS_SENT and r.due_date == inv.due_date for r in rows):
                continue
            day = planned_day(inv.due_date, today, cfg)
            if day is None:
                continue
            deferred.pop(inv.id, None)
            reason = followup.exclusion(inv, inv.customer)
            if reason:
                pending_skips.append((inv, reason))
            else:
                planned.append((inv, day))
    # Ciò che il giro non rivaluterà (manca il promemoria per questa scadenza,
    # fattura non ancora scaduta…) resta com'è scritto nel registro.
    final_skips.extend(deferred.values())

    items: List[dict] = []
    if sent:
        days = set()
        lines = []
        for group in sent.values():
            moments = [event_time(r) for _i, r in group if event_time(r)]
            when = max(moments) if moments else None
            days.add(_local(when).date() if when else None)
            numbers = ", ".join(invoice_label(i.invoice_number) for i, _r in group)
            to = group[0][1].to_email
            lines.append(f"Fattur{'a' if len(group) == 1 else 'e'} {numbers}: secondo avviso inviato "
                         f"{_when_text(when)}{' a ' + to if to else ''}")
        known = [d for d in days if d is not None]
        items.append(_item(
            KIND_FOLLOWUP, SENT, _counted(len(sent), days, "2° avviso", "secondi avvisi", today), lines,
            max(known) if known else None, len(sent),
        ))
    if stuck:
        days = set()
        lines = []
        for group in stuck.values():
            moments = [event_time(r) for _i, r in group if event_time(r)]
            when = max(moments) if moments else None
            days.add(_local(when).date() if when else None)
            numbers = ", ".join(invoice_label(i.invoice_number) for i, _r in group)
            lines.append(f"Fattur{'a' if len(group) == 1 else 'e'} {numbers}: invio non riuscito {_when_text(when)}")
        known = [d for d in days if d is not None]
        items.append(_item(
            KIND_FOLLOWUP, STUCK, _counted(len(stuck), days, "2° avviso fermo", "secondi avvisi fermi", today),
            lines + ["Da gestire in Sistema → «Secondo avviso email» → Da gestire."],
            max(known) if known else None, len(stuck),
        ))
    if planned or retrying:
        if planned:
            first = min(day for _i, day in planned)
            label = f"2° avviso previsto {short_date(first, today)}"
            lines = [f"Fattura {invoice_label(i.invoice_number)}: secondo avviso previsto il {day.strftime('%d/%m/%Y')}"
                     for i, day in planned]
            lines.append("Parte nel giro del mattino, solo se la fattura risulta ancora da saldare.")
        else:
            first = None
            label = "2° avviso: nuovo tentativo al prossimo giro"
            lines = []
        lines += [f"Fattura {invoice_label(i.invoice_number)}: invio non riuscito, il giro ritenta da solo" for i in retrying]
        items.append(_item(KIND_FOLLOWUP, PLANNED, label, lines, first, len(planned) + len(retrying)))
    seen = set()
    for pairs, pending in ((final_skips, False), (pending_skips, True)):
        for reason, invoices in _by_reason(pairs).items():
            state = reason_state(reason)
            if state == PAUSED:
                label = f"2° avviso sospeso: {reason_text(reason)}"
            elif pending:
                label = f"2° avviso non previsto: {reason_text(reason)}"
            else:
                label = f"2° avviso non partito: {reason_text(reason)}"
            if label in seen:
                continue
            seen.add(label)
            verb = "non partirà" if pending else "non inviato"
            items.append(_item(
                KIND_FOLLOWUP, state, label,
                (f"Fattura {invoice_label(i.invoice_number)}: secondo avviso {verb} ({reason_text(reason)})" for i in invoices),
                None, len(invoices),
            ))
    return items


def customer_status(session: Session, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Per ogni cliente con almeno un'email automatica su una fattura ancora
    da saldare: le voci da mostrare sotto il suo nome, nell'ordine di lettura
    (promemoria, poi secondo avviso). {customers: {id: [voce]}, followup_mode}."""
    today = italian_today(now_utc)
    mode = followup.resolve_followup_mode(cfg)[0]
    reminders = _open_rows(session, InvoiceReminderEmail)
    followups, readable = _safely(session, lambda: _open_rows(session, InvoiceFollowupEmail), {})
    per_customer: Dict[int, Dict[str, list]] = defaultdict(lambda: {"rem": [], "fol": []})
    for inv, rows in reminders.values():
        per_customer[inv.customer_id]["rem"].append((inv, rows))
    for inv, rows in followups.values():
        per_customer[inv.customer_id]["fol"].append((inv, rows))
    order = {key: n for n, key in enumerate(ITEM_ORDER)}
    customers: Dict[int, List[dict]] = {}
    for customer_id, found in per_customer.items():
        items = _reminder_items(found["rem"], today) if found["rem"] else []
        items += _followup_items(found["rem"], found["fol"], today, mode, now_utc, cfg)
        if items:
            items.sort(key=lambda it: order.get((it["kind"], it["state"]), len(order)))
            customers[customer_id] = items
    return {"customers": customers, "followup_mode": mode, "followup_readable": readable, "today": today.isoformat()}


# ── Registro «Email inviate» ───────────────────────────────────────

def _log_state(kind: str, row, inv: Optional[Invoice], mode: str, now_utc: Optional[datetime], cfg) -> Tuple[str, str]:
    """(stato, etichetta) di una riga nel registro."""
    if row.status == STATUS_SENT:
        return SENT, "Inviata"
    if row.status == STATUS_SKIPPED:
        state = reason_state(row.skip_reason)
        return state, f"{'Sospesa' if state == PAUSED else 'Non partita'}: {reason_text(row.skip_reason)}"
    if inv is None or not followup.still_due(inv):
        return CLOSED, "Non riuscita: fattura non più da saldare"
    if kind == KIND_REMINDER:
        fate = reminder_trouble(row, italian_today(now_utc))
        if fate == LOST:
            return MISSING, "Non partita: invio non riuscito"
        if fate == UNCERTAIN:
            return MISSING, "Esito incerto: non più ritentabile"
        return STUCK, "Ferma: da gestire"
    current = row.due_date == inv.due_date
    if mode == MODE_REALE and current and followup.will_auto_retry(row, inv, MODE_REALE, now_utc, cfg):
        return PLANNED, "Non riuscita: il giro ritenta da solo"
    return STUCK, "Ferma: da gestire"


def _survivor_id(session: Session, customer: Optional[Customer]) -> Optional[int]:
    """La scheda da aprire oggi: quella a cui il cliente è stato unito, se lo è stato."""
    seen = set()
    while customer is not None and customer.merged_into is not None and customer.id not in seen:
        seen.add(customer.id)
        customer = session.get(Customer, customer.merged_into)
    return customer.id if customer is not None else None


def _entries(session: Session, kind: str, found: list, mode: str, now_utc: Optional[datetime], cfg) -> List[dict]:
    """Le righe (riga, fattura, cliente) come voci del registro: un promemoria
    è una voce; le fatture di uno stesso secondo avviso sono una voce sola."""
    groups: Dict[tuple, list] = {}
    for row, inv, customer in found:
        state, label = _log_state(kind, row, inv, mode, now_utc, cfg)
        key = (_email_key(row) if kind == KIND_FOLLOWUP else f"riga-{row.id}", state, label)
        groups.setdefault(key, []).append((row, inv, customer))
    out: List[dict] = []
    for (_key, state, label), members in groups.items():
        members.sort(key=lambda m: (m[0].due_date or date.min, m[0].id))
        first_row, _first_inv, customer = members[0]
        moments = [event_time(r) for r, _i, _c in members if event_time(r)]
        when = _local(max(moments)) if moments else None
        errors = [r.error for r, _i, _c in members if r.error and state not in (SENT, PAUSED, MISSING)]
        out.append({
            "key": f"{kind}-{first_row.id}",
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "at": when.isoformat() if when else None,
            "day": when.date().isoformat() if when else None,
            "time": when.strftime("%H:%M") if when else None,
            "customer_id": _survivor_id(session, customer),
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
            "cc": list(first_row.cc_json or []),
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
        .filter(model.mode == MODE_REALE)
    )


def _sort_key(entry: dict):
    return (entry["at"] or "", entry["kind"], entry["key"])


def sent_log(session: Session, days: int = 14, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Promemoria e secondi avvisi REALI degli ultimi `days` giorni, insieme e
    dal più recente, raggruppati per giorno italiano; più TUTTE le email ferme
    di fatture ancora da saldare (qualunque età: sono l'elenco «da gestire»)."""
    days = max(MIN_LOG_DAYS, min(int(days or MIN_LOG_DAYS), MAX_LOG_DAYS))
    today = italian_today(now_utc)
    first_day = today - timedelta(days=days - 1)
    since = (
        datetime.combine(first_day, time.min, tzinfo=ZoneInfo(config.TIMEZONE))
        .astimezone(timezone.utc).replace(tzinfo=None)
    )
    mode = followup.resolve_followup_mode(cfg)[0]
    entries: List[dict] = []
    stuck: List[dict] = []
    readable = True

    def read(model, kind):
        moment = func.coalesce(model.sent_at, model.attempted_at, model.created_at)
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
            [e for e in _entries(session, kind, recent, mode, now_utc, cfg) if e["day"] and e["day"] >= first_day.isoformat()],
            [e for e in _entries(session, kind, halted, mode, now_utc, cfg) if e["state"] == STUCK],
        )

    found, halted = read(InvoiceReminderEmail, KIND_REMINDER)
    entries.extend(found)
    stuck.extend(halted)
    (found, halted), readable = _safely(session, lambda: read(InvoiceFollowupEmail, KIND_FOLLOWUP), ([], []))
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
