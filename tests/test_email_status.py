"""Stato delle email automatiche accanto al nome del cliente e registro
«Email inviate» (backend/engine/email_status.py, API /api/reminders/email/
customer-status e /email/sent).

Richiesta owner 24–26/11/2026: la piattaforma deve dire, accanto al nome del
cliente, quali email sono partite e quando — per non sollecitare due volte e
per non perdere per strada chi non ha ricevuto nulla. Qui ogni regola di
lettura ha il suo test: solo invii reali, solo fatture ancora da saldare,
solo le email di QUEL cliente, nessuna promessa che il giro non manterrà,
mai «non partito» per ciò che potrebbe essere arrivato, mai «da gestire» per
ciò che non si può più gestire.
"""
import random
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from backend.connectors.resend_mail import MailSendError
from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail, SyncState
from backend.engine import email_status as es
from backend.engine import followup_email as fu
from backend.engine import reminder_email as rem
from backend.engine.email_status import customer_status, first_round_day, planned_day, sent_log

# Orologio di partenza: mercoledì 18/11/2026 alle 08:00 italiane, PRIMA del
# giro delle 09:15. Scadenza 11/11 = scaduta da 7 giorni esatti: parte oggi.
EARLY = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)
# Lo stesso giorno alle 11:00 italiane: il giro di oggi è passato.
LATE = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 18)
DUE7 = date(2026, 11, 11)
CLOCK = {"now": EARLY}


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        now = CLOCK["now"]
        return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return CLOCK["now"].replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    CLOCK["now"] = EARLY
    monkeypatch.setattr("backend.engine.format.datetime", _FixedDatetime)
    monkeypatch.setattr("backend.engine.reminder_email.datetime", _FixedDatetime)
    monkeypatch.setattr("backend.engine.followup_email.datetime", _FixedDatetime)


def _cfg(**over):
    base = dict(
        REMINDER_EMAIL_ENABLED=False, RESEND_API_KEY="", RESEND_API_URL="https://api.resend.test",
        REMINDER_EMAIL_FROM="Sake Company <fatture@sakecompany.com>",
        REMINDER_EMAIL_REPLY_TO="fatture@sakecompany.com", REMINDER_EMAIL_BCC="",
        REMINDER_EMAIL_TEST_TO="", REMINDER_EMAIL_HOUR=9, REMINDER_EMAIL_MINUTE=15,
        REMINDER_EMAIL_CC="fatture@sakecompany.com",
        REMINDER_AGENT_EMAILS="Maggioni:g.maggioni@sakecompany.com;Izzo:a.izzo@sakecompany.com;Scotti:g.scotti@sakecompany.com",
        TIMEZONE="Europe/Rome",
        REMINDER_FOLLOWUP_ENABLED=False, REMINDER_FOLLOWUP_DAYS="7", REMINDER_FOLLOWUP_LOOKBACK_DAYS="6",
        REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS="3",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _off():
    """Promemoria reale, secondo avviso SPENTO (com'è oggi in produzione)."""
    return _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_test_key")


def _on(**over):
    """Secondo avviso ACCESO in modalità reale."""
    return _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_test_key", REMINDER_FOLLOWUP_ENABLED=True, **over)


def _cust(s, nome="Shabu Casatenovo", email="amministrazione@shabu.it", **kw):
    c = Customer(ragione_sociale=nome, email=email, partita_iva=kw.pop("partita_iva", "12345670785"),
                 excluded=kw.pop("excluded", False), recovery_status=kw.pop("recovery_status", "idle"),
                 source="shopify", **kw)
    s.add(c)
    s.commit()
    return c


def _inv(s, cust, num="2026/00001804/SAK - Fattura", amount=426.76, due=DUE7, status="open", **kw):
    i = Invoice(
        invoice_number=num, amount=amount, amount_due=kw.pop("amount_due", amount),
        issue_date=due - timedelta(days=30), due_date=due, due_date_source=kw.pop("due_date_source", "real"),
        days_overdue=max((TODAY - due).days, 0), customer_id=cust.id if cust else None,
        source_platform="fatturapro", status=status,
        customer_name_raw=cust.ragione_sociale if cust else None,
        customer_piva_raw=cust.partita_iva if cust else None, **kw,
    )
    s.add(i)
    s.commit()
    return i


def _rem(s, inv, status="sent", mode="reale", due=None, when=None, customer_id="same", **kw):
    """Riga del registro dei promemoria. Inviata: alle 09:15 italiane del
    giorno prima della scadenza, come fa il giro."""
    due = due or inv.due_date
    moment = when or datetime(due.year, due.month, due.day, 8, 15) - timedelta(days=1)
    row = InvoiceReminderEmail(
        invoice_id=inv.id, customer_id=inv.customer_id if customer_id == "same" else customer_id,
        due_date=due, status=status, mode=mode, to_email=kw.pop("to_email", "amministrazione@shabu.it"),
        subject="Promemoria", amount_due=inv.amount_due,
        sent_at=kw.pop("sent_at", moment if status == "sent" else None),
        attempted_at=kw.pop("attempted_at", moment if status in ("sent", "failed", "sending") else None),
        created_at=moment, **kw,
    )
    s.add(row)
    s.commit()
    return row


def _fol(s, inv, status="sent", mode="reale", due=None, when=None, group=None, **kw):
    """Riga del registro dei secondi avvisi (di default: inviata oggi alle 09:16 italiane)."""
    due = due or inv.due_date
    moment = when or datetime(2026, 11, 18, 8, 16)
    row = InvoiceFollowupEmail(
        invoice_id=inv.id, customer_id=kw.pop("customer_id", inv.customer_id), due_date=due, status=status,
        mode=mode, to_email=kw.pop("to_email", "amministrazione@shabu.it"), subject="Sollecito",
        amount_due=inv.amount_due, group_key=group,
        sent_at=moment if status == "sent" else None,
        attempted_at=kw.pop("attempted_at", moment if status in ("sent", "failed", "sending") else None),
        created_at=moment, **kw,
    )
    s.add(row)
    s.commit()
    return row


def _status(s, cfg=None, now=EARLY):
    return customer_status(s, now_utc=now, cfg=cfg or _off())


def _items(s, cust, cfg=None, now=EARLY):
    return _status(s, cfg, now)["customers"].get(cust.id, [])


def _labels(s, cust, cfg=None, now=EARLY):
    return [it["label"] for it in _items(s, cust, cfg, now)]


def _pairs(s, cust, cfg=None, now=EARLY):
    return [(it["state"], it["label"]) for it in _items(s, cust, cfg, now)]


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


# ── Date, giro e motivi ────────────────────────────────────────────

class TestRules:
    def test_planned_day_is_the_engine_window_seen_from_the_invoice(self):
        """Per ogni scadenza e ogni giorno: planned_day dà QUEL giorno se e
        solo se il giro di quel giorno ha la scadenza nella sua finestra."""
        cfg = _on()
        first = date(2026, 10, 20)
        for offset in range(60):
            day = first + timedelta(days=offset)
            window = fu.followup_window(_at(day, 10), cfg)
            for back in range(0, 25):
                due = day - timedelta(days=back)
                in_window = bool(window and window[0] <= due <= window[1])
                assert (planned_day(due, day, cfg) == day) is in_window, (day, due)

    def test_planned_day_examples(self):
        cfg = _on()
        assert planned_day(DUE7, TODAY, cfg) == TODAY                           # 7 giorni esatti: oggi
        assert planned_day(date(2026, 11, 12), TODAY, cfg) == date(2026, 11, 19)  # domani
        assert planned_day(date(2026, 11, 14), TODAY, cfg) == date(2026, 11, 23)  # sabato → lunedì
        assert planned_day(date(2026, 11, 5), TODAY, cfg) == TODAY              # ultimo giorno di recupero
        assert planned_day(date(2026, 11, 4), TODAY, cfg) is None               # finestra passata
        assert planned_day(None, TODAY, cfg) is None

    def test_planned_day_follows_the_configured_days(self):
        cfg = _on(REMINDER_FOLLOWUP_DAYS="10", REMINDER_FOLLOWUP_LOOKBACK_DAYS="3")
        assert planned_day(date(2026, 11, 9), TODAY, cfg) == date(2026, 11, 19)
        assert planned_day(date(2026, 11, 4), TODAY, cfg) is None

    @pytest.mark.parametrize("now,expected", [
        (datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc), date(2026, 11, 18)),    # 08:00: oggi
        (datetime(2026, 11, 18, 8, 14, tzinfo=timezone.utc), date(2026, 11, 18)),   # 09:14: oggi
        (datetime(2026, 11, 18, 8, 15, tzinfo=timezone.utc), date(2026, 11, 18)),   # 09:15: il giro sta partendo
        (datetime(2026, 11, 18, 8, 24, tzinfo=timezone.utc), date(2026, 11, 18)),   # 09:24: ancora nel margine
        (datetime(2026, 11, 18, 8, 25, tzinfo=timezone.utc), date(2026, 11, 19)),   # 09:25: per oggi è passato
        (datetime(2026, 11, 18, 22, 30, tzinfo=timezone.utc), date(2026, 11, 19)),  # 23:30: domani
        (datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc), date(2026, 11, 19)),  # 00:30 del 19: è già «oggi»
        (datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc), date(2026, 11, 23)),   # venerdì dopo il giro: lunedì
        (datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc), date(2026, 11, 23)),    # sabato mattina: lunedì
        (datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc), date(2026, 11, 23)),   # domenica sera: lunedì
    ])
    def test_first_round_day(self, now, expected):
        assert first_round_day(now, _on()) == expected

    def test_first_round_day_follows_the_configured_hour(self):
        assert first_round_day(LATE, _on(REMINDER_EMAIL_HOUR=12, REMINDER_EMAIL_MINUTE=0)) == TODAY
        assert first_round_day(EARLY, _on(REMINDER_EMAIL_HOUR=7, REMINDER_EMAIL_MINUTE=30)) == date(2026, 11, 19)
        assert first_round_day(EARLY, _on(REMINDER_EMAIL_HOUR=7, REMINDER_EMAIL_MINUTE=51)) == TODAY  # 08:00 < 08:01

    def test_first_round_day_reads_the_clock_when_no_time_is_given(self):
        CLOCK["now"] = LATE
        assert first_round_day(None, _on()) == date(2026, 11, 19)
        CLOCK["now"] = EARLY
        assert first_round_day(None, _on()) == TODAY

    def test_short_date_adds_the_year_only_when_it_differs(self):
        assert es.short_date(date(2026, 11, 7), TODAY) == "07/11"
        assert es.short_date(date(2025, 12, 30), TODAY) == "30/12/25"
        assert es.short_date(None, TODAY) == ""

    def test_reasons(self):
        assert es.reason_state("in_attesa") == es.PAUSED and es.reason_state("avvocato") == es.PAUSED
        assert es.reason_state("senza_email") == es.MISSING
        assert es.reason_state("abbinamento_da_verificare") == es.MISSING
        # Un motivo che il registro non conosce ancora: nel dubbio si segnala.
        assert es.reason_state("motivo_nuovo") == es.MISSING and es.reason_state(None) == es.MISSING
        # Un mancato invio già avvenuto: un fatto (grigio) o un dato da sistemare (rosso), mai «sospeso».
        assert es.fact_state("in_attesa") == es.CLOSED and es.fact_state("cliente_escluso") == es.CLOSED
        assert es.fact_state("senza_email") == es.MISSING and es.fact_state(None) == es.MISSING
        assert es.reason_text("motivo_nuovo") == "motivo nuovo"
        assert es.reason_text(None) == "motivo non registrato"

    def test_every_skip_reason_of_the_engines_has_short_words(self):
        from backend.engine.reminder_email import SKIP_LABELS
        assert set(SKIP_LABELS) <= set(es.REASON_SHORT)
        assert es.PAUSE_REASONS <= set(SKIP_LABELS)

    def test_event_time_is_the_send_for_sent_rows_and_the_attempt_otherwise(self):
        sent = SimpleNamespace(status="sent", sent_at=datetime(2026, 11, 10, 8, 15),
                               attempted_at=datetime(2026, 11, 10, 8, 14), created_at=datetime(2026, 11, 9))
        failed = SimpleNamespace(status="failed", sent_at=datetime(2026, 10, 1), attempted_at=datetime(2026, 11, 12, 9),
                                 created_at=datetime(2026, 11, 10))
        skipped = SimpleNamespace(status="skipped", sent_at=None, attempted_at=None, created_at=datetime(2026, 11, 10))
        assert es.event_time(sent) == datetime(2026, 11, 10, 8, 15)
        assert es.event_time(failed) == datetime(2026, 11, 12, 9)  # un vecchio sent_at non conta
        assert es.event_time(skipped) == datetime(2026, 11, 10)

    def test_event_day_is_the_italian_day(self):
        late = SimpleNamespace(status="sent", sent_at=datetime(2026, 11, 16, 23, 30), attempted_at=None, created_at=None)
        assert es.event_day(late) == date(2026, 11, 17)  # 23:30 UTC = 00:30 italiane del giorno dopo
        assert es.event_day(SimpleNamespace(status="skipped", sent_at=None, attempted_at=None, created_at=None)) is None

    def test_a_row_was_attempted_if_it_has_an_attempt_time_or_is_stuck(self):
        row = lambda **kw: SimpleNamespace(**dict(dict(status="skipped", attempted_at=None), **kw))  # noqa: E731
        assert not es.was_attempted(row())
        assert es.was_attempted(row(attempted_at=datetime(2026, 11, 10, 8, 15)))   # tentata, poi saltata
        assert es.was_attempted(row(status="failed")) and es.was_attempted(row(status="sending"))

    def test_a_row_attempted_today_after_the_round_time_has_had_todays_round(self):
        """La prova batte l'orologio: per QUESTA riga il giro di oggi è passato,
        anche se il margine direbbe ancora «oggi»."""
        cfg = _on()
        row = lambda moment: SimpleNamespace(attempted_at=moment)  # noqa: E731
        tomorrow = date(2026, 11, 19)
        assert es.start_after(row(datetime(2026, 11, 18, 8, 16)), TODAY, TODAY, cfg) == tomorrow   # 09:16 di oggi
        assert es.start_after(row(datetime(2026, 11, 18, 8, 15)), TODAY, TODAY, cfg) == tomorrow   # 09:15 in punto
        assert es.start_after(row(datetime(2026, 11, 18, 7, 0)), TODAY, TODAY, cfg) == TODAY       # 08:00: giro a mano prima dell'ora
        assert es.start_after(row(datetime(2026, 11, 17, 8, 16)), TODAY, TODAY, cfg) == TODAY      # ieri
        assert es.start_after(row(None), TODAY, TODAY, cfg) == TODAY
        assert es.start_after(row(datetime(2026, 11, 18, 8, 16)), TODAY, tomorrow, cfg) == tomorrow
        friday = date(2026, 11, 20)
        assert es.start_after(row(datetime(2026, 11, 20, 8, 16)), friday, friday, cfg) == date(2026, 11, 23)

    def test_a_row_being_sent_right_now_is_in_flight(self):
        now = datetime(2026, 11, 18, 8, 16)
        row = lambda **kw: SimpleNamespace(**dict(dict(status="sending", attempted_at=now - timedelta(minutes=1)), **kw))  # noqa: E731
        assert es.in_flight(row(), now)
        assert not es.in_flight(row(attempted_at=now - timedelta(minutes=3)), now)    # orfana di un giro caduto
        assert not es.in_flight(row(attempted_at=now + timedelta(minutes=1)), now)
        assert not es.in_flight(row(status="failed"), now) and not es.in_flight(row(attempted_at=None), now)

    def test_each_invoice_leaves_with_the_first_email_that_takes_it(self):
        """Una email per cliente, a ogni giro: la prima porta con sé le fatture a
        un giorno dalla soglia; le altre fanno lo stesso tra loro."""
        cfg = _on()
        inv = lambda n, due: SimpleNamespace(id=n, due_date=due)  # noqa: E731
        a, b, c = inv(1, date(2026, 11, 9)), inv(2, date(2026, 11, 13)), inv(3, date(2026, 11, 14))
        monday = date(2026, 11, 16)
        own = [(i, planned_day(i.due_date, monday, cfg)) for i in (a, b, c)]
        assert [day for _i, day in own] == [monday, date(2026, 11, 20), date(2026, 11, 23)]
        assert es.leave_days(own, cfg) == {1: monday, 2: date(2026, 11, 20), 3: date(2026, 11, 20)}
        assert es.leave_days([], cfg) == {}

    def test_a_reminder_can_be_retried_until_its_due_date(self):
        row = lambda due: SimpleNamespace(due_date=due)  # noqa: E731
        assert es.reminder_retriable(row(TODAY), TODAY) and es.reminder_retriable(row(TODAY + timedelta(days=1)), TODAY)
        assert not es.reminder_retriable(row(TODAY - timedelta(days=1)), TODAY)
        assert not es.reminder_retriable(row(None), TODAY)


class TestMergedCards:
    def test_same_customer_agrees_with_the_engine_on_random_merge_graphs(self, test_db_session):
        """La mappa letta una volta deve dare le stesse risposte della regola
        del motore (una query per passaggio), cicli compresi."""
        rng = random.Random(7)
        customers = [_cust(test_db_session, f"Cliente {n}", f"c{n}@x.it", partita_iva=f"{n:011d}") for n in range(1, 13)]
        ids = [c.id for c in customers]
        for c in customers:
            if rng.random() < 0.6:
                c.merged_into = rng.choice(ids)  # anche su sé stesso o in ciclo
        test_db_session.commit()
        merged = es.merge_map(test_db_session)
        for a in ids + [None, 9999]:
            for b in ids + [None, 9999]:
                assert es.same_customer(merged, a, b) is fu._is_same_customer(test_db_session, a, b), (a, b)

    def test_merge_map_lists_only_merged_cards(self, test_db_session):
        a = _cust(test_db_session, "A", "a@x.it")
        b = _cust(test_db_session, "B", "b@x.it", partita_iva="00000000002", merged_into=a.id)
        assert es.merge_map(test_db_session) == {b.id: a.id}

    def test_survivor(self):
        assert es.survivor_id({2: 1, 3: 2}, 3) == 1 and es.survivor_id({2: 1}, 1) == 1
        assert es.survivor_id({}, None) is None
        assert es.survivor_id({2: 99}, 2) == 99          # scheda di arrivo che non c'è più: si dice dove puntava
        assert es.survivor_id({1: 2, 2: 1}, 1) in (1, 2)  # un ciclo non blocca la pagina


# ── Promemoria accanto al nome ─────────────────────────────────────

class TestReminderNextToTheName:
    def test_nothing_without_emails(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c)
        data = _status(test_db_session)
        assert data["customers"] == {} and data["followup_mode"] == "off" and data["followup_readable"] is True
        assert data["today"] == "2026-11-18" and data["next_round"] == "2026-11-18"
        assert data["read_at"] == "08:00"   # le 07:00 UTC

    def test_one_reminder_sent(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        [item] = _items(test_db_session, c)
        assert (item["kind"], item["state"], item["label"]) == ("promemoria", "sent", "Promemoria 10/11")
        assert item["date"] == "2026-11-10" and item["count"] == 1
        assert item["title"] == ("Fattura 2026/00001804/SAK: promemoria inviato il 10/11/2026 alle 09:15 "
                                 "a amministrazione@shabu.it")

    def test_a_sent_row_without_an_attempt_time_is_still_just_sent(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), attempted_at=None)
        assert _pairs(test_db_session, c) == [("sent", "Promemoria 10/11")]

    def test_two_reminders_the_same_day(self, test_db_session):
        c = _cust(test_db_session)
        for num in ("2026/00001804/SAK", "2026/00001805/SAK"):
            _rem(test_db_session, _inv(test_db_session, c, num))
        [item] = _items(test_db_session, c)
        assert item["label"] == "2 promemoria 10/11" and item["count"] == 2
        assert item["title"].count("\n") == 1

    def test_two_reminders_on_different_days(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK", due=date(2026, 11, 13)))
        [item] = _items(test_db_session, c)
        assert item["label"] == "2 promemoria, ultimo 12/11" and item["date"] == "2026-11-12"

    def test_the_day_is_the_italian_one(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), when=datetime(2026, 11, 9, 23, 30))
        assert _labels(test_db_session, c) == ["Promemoria 10/11"]

    def test_an_email_of_another_year_shows_the_year(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2025, 12, 20)))
        assert _labels(test_db_session, c) == ["Promemoria 19/12/25"]

    @pytest.mark.parametrize("mode", ["prova", "test"])
    def test_a_trial_is_not_an_email_to_the_customer(self, test_db_session, mode):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), mode=mode)
        assert _items(test_db_session, c) == []

    @pytest.mark.parametrize("status,amount_due", [("paid", 0.0), ("void", 426.76), ("disputed", 426.76), ("open", 0.0)])
    def test_only_invoices_still_to_be_paid(self, test_db_session, status, amount_due):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, status=status, amount_due=amount_due))
        assert _items(test_db_session, c) == []

    def test_a_promised_invoice_is_still_to_be_paid(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, status="promised"))
        assert _labels(test_db_session, c) == ["Promemoria 10/11"]

    def test_the_email_sent_to_another_customer_is_not_shown(self, test_db_session):
        """Fattura riabbinata dopo l'invio: il nuovo cliente non ha ricevuto nulla."""
        old = _cust(test_db_session, "Vecchio Srl", "info@vecchio.it", partita_iva="01234567897")
        new = _cust(test_db_session, "Nuovo Srl", "info@nuovo.it")
        inv = _inv(test_db_session, new)
        _rem(test_db_session, inv, customer_id=old.id)
        assert _status(test_db_session)["customers"] == {}

    def test_a_merged_card_is_the_same_customer(self, test_db_session):
        survivor = _cust(test_db_session, "Shabu Casatenovo")
        merged = _cust(test_db_session, "Shabu doppione", "x@shabu.it", partita_iva="01234567897",
                       merged_into=survivor.id)
        inv = _inv(test_db_session, survivor)
        _rem(test_db_session, inv, customer_id=merged.id)
        assert _labels(test_db_session, survivor) == ["Promemoria 10/11"]

    def test_reminder_not_sent_for_a_missing_email(self, test_db_session):
        c = _cust(test_db_session, email=None)
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="senza_email")
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("missing", "Promemoria non partito: senza email")
        assert item["date"] is None and "secondo avviso automatico non parte" in item["title"]
        assert "oggi non più" not in item["title"]

    def test_several_reminders_not_sent_for_the_same_reason(self, test_db_session):
        c = _cust(test_db_session, email=None)
        for num in ("2026/00001804/SAK", "2026/00001805/SAK"):
            _rem(test_db_session, _inv(test_db_session, c, num), status="skipped", skip_reason="senza_email")
        [item] = _items(test_db_session, c)
        assert item["label"] == "2 promemoria non partiti: senza email" and item["count"] == 2

    def test_a_reminder_held_by_a_choice_is_a_plain_fact(self, test_db_session):
        c = _cust(test_db_session, recovery_status="lawyer")
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="avvocato")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK"), status="skipped", skip_reason="avvocato")
        assert _pairs(test_db_session, c) == [("closed", "2 promemoria non partiti: pratica dall'avvocato")]

    def test_an_excluded_customer(self, test_db_session):
        c = _cust(test_db_session, excluded=True)
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="cliente_escluso")
        assert _pairs(test_db_session, c) == [("closed", "Promemoria non partito: cliente escluso")]

    def test_the_written_reason_is_told_as_a_fact_whatever_is_true_today(self, test_db_session):
        """Il motivo è quello scritto dal giro quel giorno: non lo si confronta
        con l'oggi (un altro motivo può coprirlo, o può non valere più) e non
        lo si dice come uno stato presente («sospeso»)."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv, status="skipped", skip_reason="forse_pagata")
        expected = [("closed", "Promemoria non partito: forse già pagata")]
        assert _pairs(test_db_session, c) == expected
        assert "scritto dal giro quel giorno" in _items(test_db_session, c)[0]["title"]
        for change in ({"missing_streak": 1}, {"missing_streak": 0}):
            for key, value in change.items():
                setattr(inv, key, value)
            test_db_session.commit()
            assert _pairs(test_db_session, c) == expected
        c.recovery_status = "lawyer"   # un motivo «più forte» oggi non cambia ciò che è successo allora
        test_db_session.commit()
        assert _pairs(test_db_session, c) == expected

    def test_an_email_added_later_does_not_rewrite_the_past(self, test_db_session):
        c = _cust(test_db_session)  # oggi l'email c'è
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="senza_email")
        assert _pairs(test_db_session, c) == [("missing", "Promemoria non partito: senza email")]

    def test_an_unregistered_reason(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), status="skipped", skip_reason=None)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK"), status="skipped", skip_reason="")
        # NULL e stringa vuota sono lo stesso (non) motivo: una voce sola.
        assert _pairs(test_db_session, c) == [("missing", "2 promemoria non partiti: motivo non registrato")]

    @pytest.mark.parametrize("status", ["failed", "sending"])
    @pytest.mark.parametrize("due", [TODAY, TODAY + timedelta(days=1)])
    def test_a_reminder_that_did_not_complete_can_be_retried_until_the_due_date(self, test_db_session, status, due):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=due), status=status, error_kind="network",
             error="ReadTimeout", when=datetime(2026, 11, 17, 8, 15))
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("stuck", "Promemoria fermo 17/11")
        assert "Sistema" in item["title"] and "verificare su Resend" in item["title"]

    @pytest.mark.parametrize("status,kind", [
        ("failed", "network"), ("failed", "provider"), ("failed", None), ("failed", "other"), ("sending", None),
        # Anche un rifiuto «certo»: il registro ricorda solo l'ULTIMO errore, e
        # dopo un «Riprova» un 409 di Resend (= l'aveva già ricevuta) è scritto
        # come «validation». Non si può dire che non è partito.
        ("failed", "validation"), ("failed", "pdf"),
    ])
    def test_after_the_due_date_a_reminder_that_did_not_complete_is_uncertain(self, test_db_session, status, kind):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status=status, error_kind=kind,
             error="Resend HTTP 409: Same idempotency key used with a different request payload")
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("missing", "Promemoria dall'esito incerto 10/11")
        assert "non partito" not in item["label"]
        assert "Non si sa se il cliente l'ha ricevuto" in item["title"] and "Resend HTTP 409" in item["title"]
        assert "il secondo avviso automatico non parte" in item["title"]

    def test_the_second_notice_is_not_denied_when_the_reminder_for_this_due_date_was_sent(self, test_db_session):
        """Timeout sul promemoria della vecchia scadenza, poi scadenza spostata
        e promemoria inviato: il secondo avviso PARTE. Non si scrive il contrario."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv, due=date(2026, 11, 10), status="failed", error_kind="network")
        _rem(test_db_session, inv)
        items = _items(test_db_session, c, _on())
        assert [it["label"] for it in items] == [
            "Promemoria 10/11", "Promemoria dall'esito incerto 09/11", "2° avviso previsto 18/11"]
        assert "secondo avviso" not in items[1]["title"] and "Non si sa se il cliente" in items[1]["title"]

    def test_an_attempt_later_rewritten_as_skipped_is_still_uncertain(self, test_db_session):
        """Timeout (forse arrivato), poi «Riprova» quando la fattura è «forse
        pagata»: il motore riscrive la riga come saltata. Non è «non partito»."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="forse_pagata",
             attempted_at=datetime(2026, 11, 10, 8, 15))
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("missing", "Promemoria dall'esito incerto 10/11")

    def test_several_uncertain_reminders(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), status="failed", error_kind="pdf")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK", due=date(2026, 11, 13)), status="sending")
        assert _labels(test_db_session, c) == ["2 promemoria dall'esito incerto, ultimo 12/11"]

    def test_due_date_moved_after_the_send(self, test_db_session):
        """Promemoria inviato per la vecchia scadenza, non partito per la nuova:
        si dicono tutti e due i fatti."""
        c = _cust(test_db_session, email="non valida")
        inv = _inv(test_db_session, c, due=date(2026, 11, 20))
        _rem(test_db_session, inv, due=DUE7)
        _rem(test_db_session, inv, status="skipped", skip_reason="email_non_valida")
        assert _labels(test_db_session, c) == ["Promemoria 10/11", "Promemoria non ancora partito: email non valida"]

    @pytest.mark.parametrize("due,label", [
        (TODAY + timedelta(days=2), "Promemoria non ancora partito: senza email"),   # lo guarda il giro di domani
        (TODAY + timedelta(days=1), "Promemoria non ancora partito: senza email"),   # lo guarda un giro di oggi
        (TODAY, "Promemoria non partito: senza email"),                               # nessun giro lo guarda più
        (TODAY - timedelta(days=1), "Promemoria non partito: senza email"),
    ])
    def test_a_skipped_reminder_can_leave_only_while_a_round_still_looks_at_it(self, test_db_session, due, label):
        """Il giro guarda solo le scadenze dei giorni dopo: saltato alle 09:15
        per l'email mancante, email aggiunta alle 10, un giro più tardi lo
        stesso giorno lo manda. Nel giorno della scadenza nessun giro lo
        guarda più: scrivere «può ancora partire» sarebbe falso."""
        c = _cust(test_db_session, email=None)
        _rem(test_db_session, _inv(test_db_session, c, due=due), status="skipped", skip_reason="senza_email",
             when=datetime(2026, 11, 17, 8, 15))
        [item] = _items(test_db_session, c, now=LATE)
        assert (item["state"], item["label"]) == ("missing", label)
        assert ("può ancora mandarlo" in item["title"]) is label.startswith("Promemoria non ancora")

    @pytest.mark.parametrize("now,due,can", [
        (_at(date(2026, 11, 20), 11), date(2026, 11, 23), True),    # venerdì: il giro guarda fino a lunedì
        (_at(date(2026, 11, 20), 11), date(2026, 11, 21), True),
        (_at(date(2026, 11, 21), 11), date(2026, 11, 23), False),   # sabato: per lunedì non c'è più nessun giro
        (_at(date(2026, 11, 22), 11), date(2026, 11, 23), False),
        (_at(date(2026, 11, 21), 11), date(2026, 11, 24), True),    # sabato, scadenza martedì: lo guarda lunedì
        (_at(date(2026, 11, 23), 11), date(2026, 11, 23), False),   # il giorno della scadenza
    ])
    def test_which_rounds_still_look_at_a_due_date(self, now, due, can):
        assert es.reminder_can_still_leave(due, now.date()) is can
        assert es.reminder_can_still_leave(None, now.date()) is False

    def test_the_round_really_sends_what_is_said_to_be_still_possible(self, test_db_session):
        """Col motore vero: saltato per l'email mancante la vigilia, email
        aggiunta, un giro più tardi lo stesso giorno lo manda. Il giorno della
        scadenza invece nessun giro lo prende."""
        s = test_db_session
        c = _cust(s, email=None)
        _inv(s, c, due=date(2026, 11, 19))
        late = _cust(s, "Tardi Srl", None, partita_iva="01234567897")
        _inv(s, late, "2026/00001900/SAK", due=date(2026, 11, 19))
        at_round = _at(TODAY, 8, 15)
        CLOCK["now"] = at_round
        rem.run_reminders(s, now_utc=at_round, mailer=_Mailer(), cfg=_off())
        assert _labels(s, c, now=LATE) == ["Promemoria non ancora partito: senza email"]
        c.email = "amministrazione@shabu.it"
        s.commit()
        CLOCK["now"] = _at(TODAY, 13)
        mailer = _Mailer()
        rem.run_reminders(s, now_utc=_at(TODAY, 13), mailer=mailer, cfg=_off())
        assert [m["to"] for m in mailer.sent] == [["amministrazione@shabu.it"]]
        assert _labels(s, c, now=_at(TODAY, 13)) == ["Promemoria 18/11"]
        # L'altro cliente sistema l'email solo il giorno della scadenza: nessun giro la guarda più.
        late.email = "info@tardi.it"
        s.commit()
        due_day = _at(date(2026, 11, 19), 8, 15)
        CLOCK["now"] = due_day
        assert _labels(s, late, now=due_day) == ["Promemoria non partito: senza email"]
        mailer = _Mailer()
        rem.run_reminders(s, now_utc=due_day, mailer=mailer, cfg=_off())
        assert mailer.sent == []

    def test_several_reminders_that_can_still_leave(self, test_db_session):
        c = _cust(test_db_session, email=None)
        for num in ("2026/00001804/SAK", "2026/00001805/SAK"):
            _rem(test_db_session, _inv(test_db_session, c, num, due=TODAY + timedelta(days=1)), status="skipped",
                 skip_reason="senza_email")
        [item] = _items(test_db_session, c)
        assert item["label"] == "2 promemoria non ancora partiti: senza email" and "può ancora mandarlo" in item["title"]

    def test_a_reminder_being_sent_right_now_is_neither_stuck_nor_uncertain(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 19)), status="sending",
             when=datetime(2026, 11, 18, 9, 59))
        assert _items(test_db_session, c, now=LATE) == []
        log = _log(test_db_session)
        assert log["stuck"] == [] and [e["state_label"] for e in log["days"][0]["items"]] == ["Invio in corso"]
        later = _at(TODAY, 10, 5)
        assert _labels(test_db_session, c, now=later) == ["Promemoria fermo 18/11"]
        assert [e["state_label"] for e in _log(test_db_session, now=later)["stuck"]] == ["Ferma: da gestire"]

    def test_the_reason_is_the_one_of_the_current_due_date(self, test_db_session):
        """Scadenza ANTICIPATA dopo un mancato invio: per la scadenza di adesso
        il promemoria è partito, il vecchio motivo non vale più."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=DUE7)
        _rem(test_db_session, inv, due=date(2026, 11, 20), status="skipped", skip_reason="senza_email",
             when=datetime(2026, 11, 3, 8, 15))
        _rem(test_db_session, inv)
        assert _labels(test_db_session, c) == ["Promemoria 10/11"]

    def test_a_skip_of_an_old_due_date_says_nothing_about_the_new_one(self, test_db_session):
        """Scadenza spostata in avanti, non ancora valutata dal giro: il motivo
        scritto per la vecchia data non descrive la nuova."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 30))
        _rem(test_db_session, inv, due=DUE7, status="skipped", skip_reason="senza_email")
        assert _items(test_db_session, c) == []

    def test_an_attempt_for_an_old_due_date_may_still_have_arrived(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 30))
        _rem(test_db_session, inv, due=date(2026, 11, 20), status="failed", error_kind="network",
             when=datetime(2026, 11, 17, 8, 15))
        # Non è «fermo»: è di una scadenza che la fattura non ha più.
        assert _pairs(test_db_session, c) == [("missing", "Promemoria dall'esito incerto 17/11")]

    def test_a_later_send_closes_an_older_problem(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 20))
        _rem(test_db_session, inv, due=DUE7, status="skipped", skip_reason="senza_email")
        _rem(test_db_session, inv)
        assert _labels(test_db_session, c) == ["Promemoria 19/11"]

    def test_sent_and_problems_in_reading_order(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001806/SAK", payment_pending="assegno"),
             status="skipped", skip_reason="in_incasso")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK", due_date_source="assumed"),
             status="skipped", skip_reason="scadenza_stimata")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001807/SAK", due=date(2026, 11, 19)), status="failed",
             error_kind="provider")
        _rem(test_db_session, _inv(test_db_session, c))
        assert [(it["kind"], it["state"], it["label"]) for it in _items(test_db_session, c)] == [
            ("promemoria", "sent", "Promemoria 10/11"),
            ("promemoria", "stuck", "Promemoria fermo 18/11"),
            ("promemoria", "missing", "Promemoria non partito: scadenza stimata"),
            ("promemoria", "closed", "Promemoria non partito: assegno in incasso"),
        ]

    def test_each_customer_gets_only_its_own(self, test_db_session):
        a = _cust(test_db_session, "Alfa Srl", "a@alfa.it")
        b = _cust(test_db_session, "Beta Srl", "b@beta.it", partita_iva="01234567897")
        _rem(test_db_session, _inv(test_db_session, a))
        _rem(test_db_session, _inv(test_db_session, b, "2026/00001900/SAK", due=date(2026, 11, 13)))
        data = _status(test_db_session)["customers"]
        assert set(data) == {a.id, b.id}
        assert data[a.id][0]["label"] == "Promemoria 10/11" and data[b.id][0]["label"] == "Promemoria 12/11"

    def test_long_tooltips_are_cut(self, test_db_session):
        c = _cust(test_db_session)
        for n in range(9):
            _rem(test_db_session, _inv(test_db_session, c, f"2026/0000190{n}/SAK"))
        [item] = _items(test_db_session, c)
        lines = item["title"].split("\n")
        assert len(lines) == es.MAX_TITLE_LINES + 1 and lines[-1] == "… e altre 3"

    def test_the_warning_is_never_cut_with_the_invoices(self, test_db_session):
        c = _cust(test_db_session, email=None)
        for n in range(8):
            _rem(test_db_session, _inv(test_db_session, c, f"2026/0000190{n}/SAK"), status="skipped",
                 skip_reason="senza_email")
        lines = _items(test_db_session, c)[0]["title"].split("\n")
        assert lines[es.MAX_TITLE_LINES] == "… e altre 2"
        assert lines[-1].endswith("Senza promemoria il secondo avviso automatico non parte.")


# ── Secondo avviso accanto al nome ─────────────────────────────────

class TestFollowupNextToTheName:
    def test_second_notice_sent_as_one_email_for_two_invoices(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "2026/00001804/SAK")
        b = _inv(test_db_session, c, "2026/00001812/SAK", 493.68, date(2026, 11, 12))
        for inv in (a, b):
            _rem(test_db_session, inv)
            _fol(test_db_session, inv, group="c1-20261118-abc")
        items = _items(test_db_session, c, now=LATE)
        assert [it["label"] for it in items] == ["2 promemoria, ultimo 11/11", "2° avviso 18/11"]
        notice = items[1]
        assert (notice["kind"], notice["state"], notice["count"], notice["date"]) == ("followup", "sent", 1, "2026-11-18")
        assert notice["title"] == ("Fatture 2026/00001804/SAK, 2026/00001812/SAK: secondo avviso inviato "
                                   "il 18/11/2026 alle 09:16 a amministrazione@shabu.it")

    def test_two_second_notices_on_different_days(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 6))
        b = _inv(test_db_session, c, "2026/00001812/SAK")
        _fol(test_db_session, a, when=datetime(2026, 11, 13, 8, 16), group="g1")
        _fol(test_db_session, b, group="g2")
        assert _labels(test_db_session, c) == ["2 secondi avvisi, ultimo 18/11"]

    def test_rows_without_a_group_are_one_email_each(self, test_db_session):
        c = _cust(test_db_session)
        _fol(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"))
        _fol(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"))
        assert _labels(test_db_session, c) == ["2 secondi avvisi 18/11"]

    @pytest.mark.parametrize("mode", ["prova", "test"])
    def test_a_trial_second_notice_is_not_shown(self, test_db_session, mode):
        c = _cust(test_db_session)
        _fol(test_db_session, _inv(test_db_session, c), mode=mode)
        assert _items(test_db_session, c) == []

    @pytest.mark.parametrize("cfg", [_off, _on])
    @pytest.mark.parametrize("status,kind", [("failed", "network"), ("failed", "provider"), ("failed", "conflict"),
                                             ("failed", None), ("sending", None)])
    def test_an_uncertain_send_is_for_the_operator(self, test_db_session, cfg, status, kind):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status=status, error_kind=kind, error="ReadTimeout")
        items = _items(test_db_session, c, cfg(), now=LATE)
        assert [(it["state"], it["label"]) for it in items] == [
            ("sent", "Promemoria 10/11"), ("stuck", "2° avviso fermo 18/11"),
        ]
        assert "Da gestire" in items[1]["title"] and "potrebbe essere arrivato" in items[1]["title"]

    def test_a_stuck_email_with_two_invoices_counts_once(self, test_db_session):
        c = _cust(test_db_session)
        for num in ("2026/00001804/SAK", "2026/00001812/SAK"):
            inv = _inv(test_db_session, c, num)
            _rem(test_db_session, inv)
            _fol(test_db_session, inv, status="sending", group="g1")
        item = _items(test_db_session, c, now=LATE)[1]
        assert (item["state"], item["label"], item["count"]) == ("stuck", "2° avviso fermo 18/11", 1)
        assert item["title"].startswith("Fatture 2026/00001804/SAK, 2026/00001812/SAK: invio dall'esito incerto")

    @pytest.mark.parametrize("kind", ["pdf", "validation", "not_attempted"])
    def test_a_certain_failure_with_a_round_ahead_is_retried_by_the_round(self, test_db_session, kind):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind=kind, error="PDF non generato",
             when=datetime(2026, 11, 17, 8, 16))
        # Mattina: il giro di oggi la riprende.
        assert _pairs(test_db_session, c, _on()) == [
            ("sent", "Promemoria 10/11"), ("planned", "2° avviso: nuovo tentativo 18/11")]
        # Dopo il giro di oggi: se è ancora ferma, la riprende quello di domani.
        assert _labels(test_db_session, c, _on(), now=LATE)[1] == "2° avviso: nuovo tentativo 19/11"
        # A secondo avviso spento nessun giro la riprende: è dell'operatore.
        assert _pairs(test_db_session, c, _off())[1] == ("stuck", "2° avviso fermo 17/11")
        assert "invio non riuscito" in _items(test_db_session, c, _off())[1]["title"]

    def test_a_certain_failure_with_no_round_left_is_for_the_operator(self, test_db_session):
        """Ultimo giorno utile: prima dell'ora del giro la riprende quello di oggi; dopo, nessuno."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 5))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 17, 8, 16))
        assert _labels(test_db_session, c, _on(), now=EARLY)[1] == "2° avviso: nuovo tentativo 18/11"
        assert _pairs(test_db_session, c, _on(), now=LATE)[1] == ("stuck", "2° avviso fermo 17/11")

    def test_a_failure_of_todays_round_is_not_promised_to_todays_round(self, test_db_session):
        """Alle 09:20 l'orologio direbbe ancora «oggi» (il giro può essere in
        corso), ma questa riga è stata tentata alle 09:16: per lei il giro di
        oggi è passato. All'ultimo giorno utile è subito «ferma, da gestire»."""
        c = _cust(test_db_session)
        last = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 5))
        _rem(test_db_session, last)
        _fol(test_db_session, last, status="failed", error_kind="validation", when=datetime(2026, 11, 18, 8, 16))
        at_0920 = _at(TODAY, 8, 20)
        assert first_round_day(at_0920, _on()) == TODAY
        assert _pairs(test_db_session, c, _on(), now=at_0920)[1] == ("stuck", "2° avviso fermo 18/11")
        log = _log(test_db_session, cfg=_on(), now=at_0920)
        assert [e["state_label"] for e in log["stuck"]] == ["Ferma: da gestire"]
        # Con altri giorni utili davanti: il prossimo giro in programma è domani.
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        inv = _inv(test_db_session, other, "2026/00001812/SAK")
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="validation", when=datetime(2026, 11, 18, 8, 16))
        assert _labels(test_db_session, other, _on(), now=at_0920)[1] == "2° avviso: nuovo tentativo 19/11"
        log = _log(test_db_session, cfg=_on(), now=at_0920)
        assert sorted(e["state_label"] for d in log["days"] for e in d["items"] if e["kind"] == "followup") == [
            "Ferma: da gestire", "Non riuscita: il giro la ritenta"]

    def test_a_row_already_tried_today_is_not_pulled_back_to_today_by_another_invoice(self, test_db_session):
        """Alle 09:20: una fattura rifiutata dal giro di oggi (riparte domani) e
        un'altra dello stesso cliente ancora «prevista oggi» per il margine.
        La prima non può partire oggi: non la si scrive insieme all'altra."""
        c = _cust(test_db_session)
        tried = _inv(test_db_session, c, "2026/00001804/SAK")
        _rem(test_db_session, tried)
        _fol(test_db_session, tried, status="failed", error_kind="validation", when=datetime(2026, 11, 18, 8, 16))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK", due=date(2026, 11, 10)))
        item = _items(test_db_session, c, _on(), now=_at(TODAY, 8, 20))[1]
        assert item["label"] == "2° avviso previsto 18/11"
        assert "2026/00001804/SAK: invio non riuscito, il giro ritenta il 19/11/2026" in item["title"]
        assert "2026/00001812/SAK: secondo avviso previsto il 18/11/2026" in item["title"]
        inv = lambda n, due: SimpleNamespace(id=n, due_date=due)  # noqa: E731
        a, b = inv(1, DUE7), inv(2, date(2026, 11, 10))
        tomorrow = date(2026, 11, 19)
        assert es.leave_days([(a, tomorrow), (b, TODAY)], _on(), {1: tomorrow}) == {1: tomorrow, 2: TODAY}
        assert es.leave_days([(a, tomorrow), (b, TODAY)], _on()) == {1: TODAY, 2: TODAY}

    def test_a_second_notice_being_sent_right_now_is_not_stuck_and_not_promised_again(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="sending", when=datetime(2026, 11, 18, 8, 16))
        at_0917 = _at(TODAY, 8, 17)
        assert _labels(test_db_session, c, _on(), now=at_0917) == ["Promemoria 10/11"]
        log = _log(test_db_session, cfg=_on(), now=at_0917)
        assert log["stuck"] == [] and [e["state_label"] for e in log["days"][0]["items"]] == ["Invio in corso"]
        assert _labels(test_db_session, c, _on(), now=LATE)[1] == "2° avviso fermo 18/11"

    def test_a_certain_failure_out_of_the_window_is_for_the_operator(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 3))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 10, 8, 16))
        assert _labels(test_db_session, c, _on())[1] == "2° avviso fermo 10/11"

    def test_without_the_reminder_nobody_can_retry(self, test_db_session):
        """Senza il promemoria per questa scadenza il giro non guarda la fattura
        e «Riprova» la rifiuta: non è «da gestire». Un rifiuto certo non ha più
        nulla da dire; un esito incerto resta da dire."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        row = _fol(test_db_session, inv, status="failed", error_kind="pdf")
        assert _items(test_db_session, c, _on()) == []
        row.error_kind = "network"
        test_db_session.commit()
        assert _pairs(test_db_session, c, _on(), now=LATE) == [("missing", "2° avviso dall'esito incerto 18/11")]

    def test_a_retry_the_round_would_skip_is_not_promised(self, test_db_session):
        """Fallita in modo certo, poi la pratica va «in attesa»: il giro non
        ritenta, salta."""
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 17, 8, 16))
        assert _pairs(test_db_session, c, _on()) == [
            ("sent", "Promemoria 10/11"), ("paused", "2° avviso sospeso: in attesa")]
        c.recovery_status = "idle"
        c.email = None
        test_db_session.commit()
        assert _pairs(test_db_session, c, _on())[1] == ("missing", "2° avviso non previsto: senza email")

    def test_planned_only_when_the_second_notice_is_on_in_real_mode(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 12)))
        assert _labels(test_db_session, c, _off()) == ["Promemoria 11/11"]
        # Acceso ma in prova (Resend non configurato): nessuna email andrà al cliente.
        assert _labels(test_db_session, c, _cfg(REMINDER_FOLLOWUP_ENABLED=True)) == ["Promemoria 11/11"]
        items = _items(test_db_session, c, _on())
        assert [it["label"] for it in items] == ["Promemoria 11/11", "2° avviso previsto 19/11"]
        assert (items[1]["state"], items[1]["date"]) == ("planned", "2026-11-19")
        assert "solo se la fattura risulta ancora da saldare" in items[1]["title"]

    def test_planned_in_test_mode_is_not_shown(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 12)))
        cfg = _on(REMINDER_EMAIL_TEST_TO="prova@sakecompany.com")
        assert fu.resolve_followup_mode(cfg)[0] == "test"
        assert _labels(test_db_session, c, cfg) == ["Promemoria 11/11"]

    def test_after_todays_round_today_is_no_longer_promised(self, test_db_session):
        """Alle 11:00 il giro delle 09:15 è passato: se la riga non c'è,
        prometterla per oggi sarebbe falso. Se ne riparla domani."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        assert _labels(test_db_session, c, _on(), now=EARLY) == ["Promemoria 10/11", "2° avviso previsto 18/11"]
        assert _labels(test_db_session, c, _on(), now=LATE) == ["Promemoria 10/11", "2° avviso previsto 19/11"]
        assert _status(test_db_session, _on(), now=LATE)["next_round"] == "2026-11-19"

    def test_on_the_last_useful_day_after_the_round_it_is_neither_promised_nor_denied(self, test_db_session):
        """Passata l'ora del giro, all'ultimo giorno utile: nessun giro è più in
        programma (non si promette), ma uno a mano può ancora mandarla (non si
        nega). Si dice ciò che è vero: non è ancora partita, e cosa si può fare."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 5)))
        assert _labels(test_db_session, c, _on(), now=EARLY) == ["Promemoria 04/11", "2° avviso previsto 18/11"]
        item = _items(test_db_session, c, _on(), now=LATE)[1]
        assert (item["state"], item["label"]) == ("stuck", "2° avviso non ancora partito: ultimo giorno utile")
        assert "«Esegui ora»" in item["title"] and "da domani non partirà più" in item["title"]
        # Dal giorno dopo è fuori dai giorni utili: una regola, non un guasto.
        tomorrow = _at(date(2026, 11, 19), 7)
        assert _pairs(test_db_session, c, _on(), now=tomorrow)[1] == ("closed", "2° avviso non previsto: giorni utili passati")

    def test_a_wait_removed_late_on_the_last_day(self, test_db_session):
        """Ultimo giorno utile: alle 09:15 il giro scrive «saltata: in attesa»;
        alle 11:00 l'operatore toglie l'attesa. Non è «prevista» (nessun giro
        in programma) e non è «non partita» (un giro a mano può mandarla)."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 5))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 18, 8, 16))
        assert _pairs(test_db_session, c, _on(), now=LATE) == [
            ("sent", "Promemoria 04/11"), ("stuck", "2° avviso non ancora partito: ultimo giorno utile")]
        # Dal giorno dopo resta il motivo scritto dal giro, come un fatto.
        tomorrow = _at(date(2026, 11, 19), 7)
        items = _items(test_db_session, c, _on(), now=tomorrow)
        assert [(it["state"], it["label"]) for it in items] == [
            ("sent", "Promemoria 04/11"), ("closed", "2° avviso non partito: in attesa")]
        assert "ultimo motivo scritto dal giro" in items[1]["title"]

    def test_planned_on_a_weekend_is_monday(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 14)))
        saturday = _at(date(2026, 11, 21), 9)
        assert _labels(test_db_session, c, _on(), now=saturday) == ["Promemoria 13/11", "2° avviso previsto 23/11"]

    @pytest.mark.parametrize("due", [TODAY, TODAY + timedelta(days=1)])
    def test_nothing_planned_before_the_invoice_is_overdue(self, test_db_session, due):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=due))
        assert [it["kind"] for it in _items(test_db_session, c, _on())] == ["promemoria"]

    def test_once_the_useful_days_have_passed_it_is_said_as_a_rule(self, test_db_session):
        """Promemoria inviato, mai un secondo avviso, giorni utili passati (anche
        perché il giro è stato bloccato per giorni): non deve sparire in
        silenzio. Vale solo a secondo avviso acceso."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 4)))
        items = _items(test_db_session, c, _on())
        assert [(it["state"], it["label"]) for it in items] == [
            ("sent", "Promemoria 03/11"), ("closed", "2° avviso non previsto: giorni utili passati")]
        assert "dal 7° al 13°, mai nel weekend" in items[1]["title"]
        assert _labels(test_db_session, c, _off()) == ["Promemoria 03/11"]
        cfg = _on(REMINDER_FOLLOWUP_DAYS="10", REMINDER_FOLLOWUP_LOOKBACK_DAYS="2")
        assert "dal 10° al 12°" in _items(test_db_session, c, cfg)[1]["title"]

    def test_useful_days_that_end_on_a_weekend(self, test_db_session):
        """Scadenza 9/11: i giorni utili arrivano a domenica 22, ma il giro non
        parte nel weekend. Sabato 21 (12 giorni) è già fuori: non si scrive
        «oltre 13 giorni», che non sarebbe vero."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 9)))
        saturday = _at(date(2026, 11, 21), 9)
        assert _labels(test_db_session, c, _on(), now=saturday)[1] == "2° avviso non previsto: giorni utili passati"

    def test_nothing_planned_without_the_reminder_for_this_due_date(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 12))
        _rem(test_db_session, inv, due=date(2026, 11, 2))  # scadenza cambiata dopo l'invio
        assert _labels(test_db_session, c, _on()) == ["Promemoria 01/11"]

    def test_nothing_planned_without_any_reminder(self, test_db_session):
        c = _cust(test_db_session, email=None)
        inv = _inv(test_db_session, c, due=date(2026, 11, 12))
        _rem(test_db_session, inv, status="skipped", skip_reason="senza_email")
        assert _labels(test_db_session, c, _on()) == ["Promemoria non partito: senza email"]

    def test_the_first_planned_day_of_the_customer(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 13)))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK", due=DUE7))
        item = _items(test_db_session, c, _on())[1]
        assert (item["label"], item["count"], item["date"]) == ("2° avviso previsto 18/11", 2, "2026-11-18")
        assert "2026/00001804/SAK: secondo avviso previsto il 20/11/2026" in item["title"]
        assert "2026/00001812/SAK: secondo avviso previsto il 18/11/2026" in item["title"]

    def test_an_invoice_one_day_behind_leaves_with_the_first(self, test_db_session):
        """Una email per cliente: la fattura a 6 giorni parte con quella a 7,
        anche se da sola sarebbe partita lunedì."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 6)))   # ven → 13/11
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK", due=date(2026, 11, 7)))   # sab → lun 16/11
        item = _items(test_db_session, c, _on(), now=_at(date(2026, 11, 12), 7))[1]
        assert item["label"] == "2° avviso previsto 13/11"
        assert item["title"].count("previsto il 13/11/2026") == 2 and "16/11/2026" not in item["title"]

    def test_three_invoices_two_emails(self, test_db_session):
        """Scadenze 9, 13 e 14/11 lette lunedì 16: oggi parte la prima; venerdì 20
        partono INSIEME la seconda e la terza (non una il 20 e una il 23)."""
        c = _cust(test_db_session)
        for num, due in (("2026/00001801/SAK", date(2026, 11, 9)), ("2026/00001802/SAK", date(2026, 11, 13)),
                         ("2026/00001803/SAK", date(2026, 11, 14))):
            _rem(test_db_session, _inv(test_db_session, c, num, due=due))
        item = [it for it in _items(test_db_session, c, _on(), now=_at(date(2026, 11, 16), 7)) if it["state"] == "planned"][0]
        assert item["label"] == "2° avviso previsto 16/11"
        assert "2026/00001801/SAK: secondo avviso previsto il 16/11/2026" in item["title"]
        assert "2026/00001802/SAK: secondo avviso previsto il 20/11/2026" in item["title"]
        assert "2026/00001803/SAK: secondo avviso previsto il 20/11/2026" in item["title"]

    def test_a_retry_today_comes_before_a_notice_planned_later(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "2026/00001804/SAK")
        _rem(test_db_session, a)
        _fol(test_db_session, a, status="failed", error_kind="validation", when=datetime(2026, 11, 17, 8, 16))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK", due=date(2026, 11, 14)))
        item = _items(test_db_session, c, _on())[1]
        assert (item["label"], item["date"], item["count"]) == ("2° avviso: nuovo tentativo 18/11", "2026-11-18", 2)
        assert "il giro ritenta il 18/11/2026" in item["title"] and "previsto il 23/11/2026" in item["title"]

    def test_a_waiting_case_is_paused_not_planned(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        _rem(test_db_session, _inv(test_db_session, c))
        assert _pairs(test_db_session, c, _on()) == [
            ("sent", "Promemoria 10/11"), ("paused", "2° avviso sospeso: in attesa")]

    def test_a_promised_invoice_is_paused(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, status="promised"))
        assert _labels(test_db_session, c, _on())[1] == "2° avviso sospeso: pagamento promesso"

    def test_planned_but_the_email_is_gone(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        c.email = None
        test_db_session.commit()
        items = _items(test_db_session, c, _on())
        assert (items[1]["state"], items[1]["label"]) == ("missing", "2° avviso non previsto: senza email")
        assert "finché vale questo motivo" in items[1]["title"]

    def test_a_skip_inside_the_window_is_read_as_of_now(self, test_db_session):
        """Il giro rivaluta ogni mattina le righe saltate ancora nei giorni
        utili: tolta l'attesa, il secondo avviso è di nuovo previsto."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 09/11", "2° avviso previsto 18/11"]
        c.recovery_status = "waiting"
        test_db_session.commit()
        assert _pairs(test_db_session, c, _on())[1] == ("paused", "2° avviso sospeso: in attesa")
        # A secondo avviso spento non c'è nessun giro: resta ciò che è scritto,
        # come un fatto, che oggi valga ancora o no.
        assert _pairs(test_db_session, c, _off())[1] == ("closed", "2° avviso non partito: in attesa")
        c.recovery_status = "idle"
        test_db_session.commit()
        assert _pairs(test_db_session, c, _off())[1] == ("closed", "2° avviso non partito: in attesa")

    def test_a_skip_out_of_the_window_is_final(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        a = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 2))
        b = _inv(test_db_session, c, "2026/00001812/SAK", due=date(2026, 11, 3))
        _fol(test_db_session, a, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 9, 8, 16))
        _fol(test_db_session, b, status="skipped", skip_reason="senza_email", when=datetime(2026, 11, 10, 8, 16))
        assert _pairs(test_db_session, c, _on()) == [
            ("missing", "2° avviso non partito: senza email"), ("closed", "2° avviso non partito: in attesa")]

    def test_two_final_skips_for_the_same_reason_are_said_once(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        for num, due in (("2026/00001804/SAK", date(2026, 11, 2)), ("2026/00001812/SAK", date(2026, 11, 3))):
            _fol(test_db_session, _inv(test_db_session, c, num, due=due), status="skipped", skip_reason="in_attesa",
                 when=datetime(2026, 11, 10, 8, 16))
        [item] = _items(test_db_session, c, _on())
        assert (item["label"], item["count"]) == ("2° avviso non partito: in attesa", 2)

    def test_a_notice_skipped_for_lack_of_the_reminder(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 2))
        _fol(test_db_session, inv, status="skipped", skip_reason="senza_promemoria", when=datetime(2026, 11, 9, 8, 16))
        assert _pairs(test_db_session, c, _on()) == [("missing", "2° avviso non partito: promemoria non inviato")]

    def test_already_sent_is_not_planned_again(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, when=datetime(2026, 11, 17, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 10/11", "2° avviso 17/11"]

    def test_stuck_is_not_planned_too(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="network", when=datetime(2026, 11, 17, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 10/11", "2° avviso fermo 17/11"]

    def test_a_trial_row_does_not_stop_the_real_plan(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, mode="prova", status="prepared")
        assert _labels(test_db_session, c, _on()) == ["Promemoria 10/11", "2° avviso previsto 18/11"]

    def test_a_second_notice_for_an_old_due_date_does_not_stop_the_new_one(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, due=date(2026, 10, 20), when=datetime(2026, 10, 27, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 09/11", "2° avviso 27/10", "2° avviso previsto 18/11"]

    def test_a_stuck_notice_of_an_old_due_date_cannot_be_handled_any_more(self, test_db_session):
        """Scadenza cambiata dopo un tentativo fermo: «Riprova» lo rifiuta e
        nessun giro lo riprende. Non è «da gestire»: se era incerto si dice
        che è incerto, se era certamente non partito non c'è nulla da dire."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, due=date(2026, 10, 20), status="failed", error_kind="pdf",
             when=datetime(2026, 10, 27, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 09/11", "2° avviso previsto 18/11"]
        other = _inv(test_db_session, c, "2026/00001812/SAK", due=date(2026, 12, 15))
        _fol(test_db_session, other, due=date(2026, 11, 9), status="failed", error_kind="network",
             when=datetime(2026, 11, 16, 8, 16))
        items = _items(test_db_session, c, _on())
        assert [(it["state"], it["label"]) for it in items] == [
            ("sent", "Promemoria 09/11"), ("planned", "2° avviso previsto 18/11"),
            ("missing", "2° avviso dall'esito incerto 16/11")]
        assert "da Sistema non si ritenta" in items[2]["title"] and "potrebbe essere arrivato" in items[2]["title"]

    def test_a_skip_the_round_will_not_reconsider_keeps_its_written_reason(self, test_db_session):
        """Saltata nei giorni utili, ma senza il promemoria per questa scadenza:
        il giro non la riprende, quindi vale ciò che è scritto."""
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16))
        assert _pairs(test_db_session, c, _on()) == [("closed", "2° avviso non partito: in attesa")]

    def test_the_second_notice_of_another_customer_is_not_shown(self, test_db_session):
        old = _cust(test_db_session, "Vecchio Srl", "info@vecchio.it", partita_iva="01234567897")
        new = _cust(test_db_session, "Nuovo Srl", "info@nuovo.it")
        inv = _inv(test_db_session, new)
        _fol(test_db_session, inv, customer_id=old.id)
        assert _status(test_db_session)["customers"] == {}

    def test_without_the_second_notice_registry_nothing_is_said_about_it(self, test_db_session, monkeypatch):
        """Registro dei secondi avvisi non leggibile: restano i promemoria, e
        NESSUNA previsione — senza sapere cosa è già partito non si promette."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv)
        real = es._open_rows
        rollbacks = []
        monkeypatch.setattr(test_db_session, "rollback", lambda: rollbacks.append(1))

        def broken(session, model, merged):
            if model is InvoiceFollowupEmail:
                session.execute(text("select * from tabella_che_non_esiste"))
            return real(session, model, merged)

        monkeypatch.setattr(es, "_open_rows", broken)
        data = _status(test_db_session, _on())
        assert data["followup_readable"] is False
        assert [it["label"] for it in data["customers"][c.id]] == ["Promemoria 10/11"]
        # Su PostgreSQL una query fallita blocca la transazione: senza
        # rollback il resto della lettura cadrebbe.
        assert rollbacks == [1]


# ── Ciò che si promette la mattina è ciò che il giro manda ─────────

class _Mailer:
    """Resend finto: `errors` = gli errori da sollevare, uno per chiamata, prima di cominciare a inviare."""

    def __init__(self, *errors):
        self.sent = []
        self.errors = list(errors)

    def send(self, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(kwargs)
        return f"msg-{len(self.sent)}"

    def close(self):
        pass


TIMEOUT = lambda: MailSendError("Resend non raggiungibile: ReadTimeout", kind="network")  # noqa: E731
ALREADY = lambda: MailSendError(  # noqa: E731
    "Resend HTTP 409: Same idempotency key used with a different request payload", kind="validation")


def _synced(s, now):
    row = s.query(SyncState).filter_by(key="invoices").first() or SyncState(key="invoices")
    row.last_sync = now.replace(tzinfo=None) - timedelta(minutes=15)
    row.result = {"fatturapro": {"success": True, "partial": False, "error": None}}
    s.add(row)
    s.commit()


class TestPromisesAgainstTheRealRound:
    def test_what_is_promised_in_the_morning_is_what_the_round_sends(self, test_db_session):
        """Tre settimane col motore vero (run_followups): ogni mattina, prima
        del giro, i clienti con «previsto oggi» devono essere esattamente
        quelli a cui il giro di quel giorno manda l'email; e dopo il giro
        nessuna promessa per oggi resta a video."""
        s = test_db_session
        cfg = _on()
        by_email = {}
        first_due = date(2026, 10, 30)
        for n in range(1, 17):
            c = _cust(s, f"Cliente {n} Srl", f"amministrazione@cliente{n}.it", partita_iva=f"{n:011d}",
                      recovery_status="waiting" if n == 5 else "idle")
            by_email[c.email] = c.id
            due = first_due + timedelta(days=n)
            _rem(s, _inv(s, c, f"2026/000019{n:02d}/SAK", 100.0 + n, due))
            if n in (3, 9):   # una seconda fattura con scadenza a un giorno
                _rem(s, _inv(s, c, f"2026/000029{n:02d}/SAK", 50.0, due + timedelta(days=1)))
            if n == 7:        # senza email dopo il promemoria
                c.email = None
                s.commit()
        paid = s.query(Invoice).filter(Invoice.invoice_number == "2026/00001911/SAK").one()
        sent_total = 0
        for offset in range(0, 24):
            day = date(2026, 11, 5) + timedelta(days=offset)
            if day == date(2026, 11, 16):
                paid.status, paid.amount_due = "paid", 0.0   # pagata prima del suo giorno
                s.commit()
            morning = _at(day, 7)
            CLOCK["now"] = morning
            _synced(s, morning)
            status = customer_status(s, now_utc=morning, cfg=cfg)["customers"]
            promised = {
                cid for cid, items in status.items() for it in items
                if it["kind"] == "followup" and it["state"] == "planned" and it["date"] == day.isoformat()
            }
            just_before = _at(day, 8, 14)   # le 09:14 italiane: stessa promessa
            CLOCK["now"] = just_before
            again = customer_status(s, now_utc=just_before, cfg=cfg)["customers"]
            assert promised == {
                cid for cid, items in again.items() for it in items
                if it["kind"] == "followup" and it["state"] == "planned" and it["date"] == day.isoformat()
            }
            round_time = _at(day, 8, 15)
            CLOCK["now"] = round_time
            mailer = _Mailer()
            fu.run_followups(s, now_utc=round_time, mailer=mailer, cfg=cfg)
            got = {by_email[m["to"][0]] for m in mailer.sent}
            assert got == promised, (day, sorted(got), sorted(promised))
            sent_total += len(mailer.sent)
            for hour, minute in ((8, 50), (11, 0), (17, 0)):   # 09:50, 12:00 e 18:00 italiane
                later = _at(day, hour, minute)
                CLOCK["now"] = later
                after = customer_status(s, now_utc=later, cfg=cfg)["customers"]
                still = [
                    (cid, it["label"]) for cid, items in after.items() for it in items
                    if it["state"] == "planned" and it["date"] == day.isoformat()
                ]
                assert still == [], (day, later, still)
            noon = _at(day, 11)
            CLOCK["now"] = noon
            for cid in got:
                assert any(it["kind"] == "followup" and it["state"] == "sent" for it in after[cid]), (day, cid)
        # 16 clienti: fuori quello in attesa, quello senza email e quello che ha pagato.
        assert sent_total == 13


class TestAgainstTheRealEngines:
    """Le righe qui non sono scritte a mano: le lasciano i motori veri
    (run_reminders, retry_row, run_followups, retry_followup_row)."""

    def _reminder_in_doubt(self, s):
        """Giro del 18/11 per una fattura che scade il 19: Resend va in timeout."""
        c = _cust(s)
        inv = _inv(s, c, due=date(2026, 11, 19))
        rem.run_reminders(s, now_utc=_at(TODAY, 8, 15), mailer=_Mailer(TIMEOUT()), cfg=_off())
        row = s.query(InvoiceReminderEmail).one()
        assert (row.status, row.error_kind) == ("failed", "network")
        return c, inv, row

    def test_a_reminder_in_doubt_is_retriable_and_riprova_accepts_it(self, test_db_session):
        c, _inv_, row = self._reminder_in_doubt(test_db_session)
        assert _pairs(test_db_session, c, now=LATE) == [("stuck", "Promemoria fermo 18/11")]
        assert [e["state_label"] for e in _log(test_db_session)["stuck"]] == ["Ferma: da gestire"]
        CLOCK["now"] = LATE
        mailer = _Mailer()
        rem.retry_row(test_db_session, row, mailer=mailer, cfg=_off())
        assert len(mailer.sent) == 1 and _labels(test_db_session, c, now=LATE) == ["Promemoria 18/11"]

    def test_a_retry_answered_already_received_is_never_shown_as_not_sent(self, test_db_session):
        """Il difetto trovato in revisione: timeout (arrivata), «Riprova», Resend
        risponde 409 e il motore scrive «validation». A scadenza passata non si
        può dire «non partito»."""
        c, _inv_, row = self._reminder_in_doubt(test_db_session)
        CLOCK["now"] = LATE
        rem.retry_row(test_db_session, row, mailer=_Mailer(ALREADY()), cfg=_off())
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "validation")
        after = _at(date(2026, 11, 20), 7)
        CLOCK["now"] = after
        [item] = _items(test_db_session, c, now=after)
        assert (item["state"], item["label"]) == ("missing", "Promemoria dall'esito incerto 18/11")
        assert "non partito" not in item["label"] and "Resend HTTP 409" in item["title"]
        log = _log(test_db_session, now=after)
        assert [e["state_label"] for d in log["days"] for e in d["items"]] == ["Esito incerto: da verificare su Resend"]
        assert log["stuck"] == []
        with pytest.raises(ValueError):   # e infatti «Riprova» non la accetta più
            rem.retry_row(test_db_session, row, mailer=_Mailer(), cfg=_off())

    def test_a_retry_that_turns_the_row_into_skipped_is_never_shown_as_not_sent(self, test_db_session):
        """Timeout (arrivata), poi la fattura sparisce da FatturaPro («forse
        pagata») e l'operatore preme «Riprova»: il motore riscrive la riga come
        saltata. Il cliente l'aveva ricevuta."""
        c, inv, row = self._reminder_in_doubt(test_db_session)
        inv.missing_streak = 1
        test_db_session.commit()
        CLOCK["now"] = LATE
        rem.retry_row(test_db_session, row, mailer=_Mailer(), cfg=_off())
        test_db_session.refresh(row)
        assert (row.status, row.skip_reason) == ("skipped", "forse_pagata") and row.attempted_at is not None
        for now in (LATE, _at(date(2026, 11, 20), 7)):
            CLOCK["now"] = now
            assert _pairs(test_db_session, c, now=now) == [("missing", "Promemoria dall'esito incerto 18/11")]
            log = _log(test_db_session, now=now)
            assert [e["state_label"] for d in log["days"] for e in d["items"]] == ["Esito incerto: da verificare su Resend"]

    def test_a_second_notice_in_doubt_is_stuck_and_riprova_accepts_it(self, test_db_session):
        s = test_db_session
        c = _cust(s)
        _rem(s, _inv(s, c))
        at_round = _at(TODAY, 8, 15)
        CLOCK["now"] = at_round
        _synced(s, at_round)
        fu.run_followups(s, now_utc=at_round, mailer=_Mailer(TIMEOUT()), cfg=_on())
        row = s.query(InvoiceFollowupEmail).one()
        assert (row.status, row.error_kind) == ("failed", "network")
        CLOCK["now"] = LATE
        assert _pairs(s, c, _on(), now=LATE)[1] == ("stuck", "2° avviso fermo 18/11")
        assert [e["state_label"] for e in _log(s, cfg=_on())["stuck"]] == ["Ferma: da gestire"]
        _synced(s, LATE)
        mailer = _Mailer()
        fu.retry_followup_row(s, row, mailer=mailer, cfg=_on(), now_utc=LATE)
        assert len(mailer.sent) == 1 and _labels(s, c, _on(), now=LATE)[1] == "2° avviso 18/11"

    def test_a_refused_second_notice_is_retried_by_the_next_round_as_promised(self, test_db_session):
        s = test_db_session
        c = _cust(s)
        _rem(s, _inv(s, c))
        at_round = _at(TODAY, 8, 15)
        CLOCK["now"] = at_round
        _synced(s, at_round)
        refused = MailSendError("Resend HTTP 422: invalid attachment", kind="validation")
        fu.run_followups(s, now_utc=at_round, mailer=_Mailer(refused), cfg=_on())
        CLOCK["now"] = LATE
        assert _pairs(s, c, _on(), now=LATE)[1] == ("planned", "2° avviso: nuovo tentativo 19/11")
        assert _log(s, cfg=_on(), now=LATE)["stuck"] == []
        next_round = _at(date(2026, 11, 19), 8, 15)
        CLOCK["now"] = next_round
        _synced(s, next_round)
        mailer = _Mailer()
        fu.run_followups(s, now_utc=next_round, mailer=mailer, cfg=_on())
        assert len(mailer.sent) == 1

    def test_on_the_last_useful_day_a_later_round_can_still_send(self, test_db_session):
        """L'altro difetto trovato in revisione. Ultimo giorno utile: alle 09:15
        il giro salta («in attesa»), alle 11:00 l'attesa viene tolta, alle 14:00
        un giro avviato a mano la manda. Alle 11:05 non si era detto né
        «prevista» né «non partita»."""
        s = test_db_session
        c = _cust(s, recovery_status="waiting")
        _rem(s, _inv(s, c, due=date(2026, 11, 5)))
        at_round = _at(TODAY, 8, 15)
        CLOCK["now"] = at_round
        _synced(s, at_round)
        mailer = _Mailer()
        fu.run_followups(s, now_utc=at_round, mailer=mailer, cfg=_on())
        assert mailer.sent == [] and s.query(InvoiceFollowupEmail).one().status == "skipped"
        c.recovery_status = "idle"
        s.commit()
        CLOCK["now"] = LATE
        item = _items(s, c, _on(), now=LATE)[1]
        assert (item["state"], item["label"]) == ("stuck", "2° avviso non ancora partito: ultimo giorno utile")
        afternoon = _at(TODAY, 13)
        CLOCK["now"] = afternoon
        _synced(s, afternoon)
        fu.run_followups(s, now_utc=afternoon, mailer=mailer, cfg=_on())
        assert len(mailer.sent) == 1
        assert _labels(s, c, _on(), now=afternoon)[1] == "2° avviso 18/11"


# ── Registro «Email inviate» ───────────────────────────────────────

def _log(s, days=14, cfg=None, now=LATE):
    return sent_log(s, days=days, now_utc=now, cfg=cfg or _off())


class TestSentLog:
    def test_empty(self, test_db_session):
        data = _log(test_db_session)
        assert data["days"] == [] and data["stuck"] == []
        assert data["totals"] == {"today_sent": 0, "week_sent": 0, "week_missing": 0, "stuck": 0}
        assert data["range"] == {"from": "2026-11-05", "to": "2026-11-18", "days": 14, "max_days": 90}
        assert data["followup_readable"] is True and data["today"] == "2026-11-18" and data["read_at"] == "11:00"

    def test_both_kinds_together_by_italian_day_newest_first(self, test_db_session):
        c = _cust(test_db_session, tags="B2B, scotti")
        a = _inv(test_db_session, c, "2026/00001804/SAK")
        b = _inv(test_db_session, c, "2026/00001812/SAK", 493.68, date(2026, 11, 12))
        for inv in (a, b):
            _rem(test_db_session, inv, cc_json=["g.scotti@sakecompany.com", "fatture@sakecompany.com"])
            _fol(test_db_session, inv, group="c1-20261118-abc", cc_json=["g.scotti@sakecompany.com"])
        data = _log(test_db_session)
        assert [(d["date"], d["label"], d["sent"]) for d in data["days"]] == [
            ("2026-11-18", "Mercoledì 18/11/2026", 1),
            ("2026-11-11", "Mercoledì 11/11/2026", 1),
            ("2026-11-10", "Martedì 10/11/2026", 1),
        ]
        notice = data["days"][0]["items"][0]
        assert (notice["kind"], notice["kind_label"], notice["time"], notice["state"], notice["state_label"]) == (
            "followup", "2° avviso", "09:16", "sent", "Inviata")
        assert [i["number"] for i in notice["invoices"]] == ["2026/00001804/SAK", "2026/00001812/SAK"]
        assert notice["amount"] == 920.44 and notice["to_email"] == "amministrazione@shabu.it"
        assert notice["cc"] == ["g.scotti@sakecompany.com"] and notice["agents"] == ["Scotti"]
        assert notice["customer_id"] == c.id and notice["ragione_sociale"] == "Shabu Casatenovo"
        assert len(notice["row_ids"]) == 2 and notice["detail"] is None
        reminder = data["days"][2]["items"][0]
        assert (reminder["kind_label"], reminder["time"], reminder["amount"]) == ("Promemoria", "09:15", 426.76)
        assert reminder["cc"] == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]
        assert reminder["invoices"] == [
            {"id": a.id, "number": "2026/00001804/SAK", "due_date": "2026-11-11", "amount_due": 426.76}]
        # Ultimi 7 giorni = dal 12/11 al 18/11: i promemoria del 10 e dell'11 sono fuori.
        assert data["totals"] == {"today_sent": 1, "week_sent": 1, "week_missing": 0, "stuck": 0}

    def test_within_a_day_the_latest_comes_first(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), when=datetime(2026, 11, 18, 8, 15))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"), when=datetime(2026, 11, 18, 8, 17))
        [day] = _log(test_db_session)["days"]
        assert [e["time"] for e in day["items"]] == ["09:17", "09:15"]

    def test_a_group_is_dated_by_its_latest_attempt(self, test_db_session):
        c = _cust(test_db_session)
        _fol(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), group="g1", when=datetime(2026, 11, 18, 8, 16))
        _fol(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"), group="g1", when=datetime(2026, 11, 18, 8, 40))
        [entry] = _log(test_db_session)["days"][0]["items"]
        assert entry["time"] == "09:40" and len(entry["invoices"]) == 2

    def test_the_same_key_on_two_customers_is_two_emails(self, test_db_session):
        a = _cust(test_db_session, "Alfa Srl", "a@alfa.it")
        b = _cust(test_db_session, "Beta Srl", "b@beta.it", partita_iva="01234567897")
        _fol(test_db_session, _inv(test_db_session, a, "ALFA/1"), group="stessa", to_email="a@alfa.it")
        _fol(test_db_session, _inv(test_db_session, b, "BETA/1"), group="stessa", to_email="b@beta.it")
        items = _log(test_db_session)["days"][0]["items"]
        assert sorted((e["ragione_sociale"], e["to_email"], len(e["invoices"])) for e in items) == [
            ("Alfa Srl", "a@alfa.it", 1), ("Beta Srl", "b@beta.it", 1)]

    @pytest.mark.parametrize("mode", ["prova", "test"])
    def test_trials_are_not_in_the_log(self, test_db_session, mode):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv, mode=mode)
        _fol(test_db_session, inv, mode=mode)
        assert _log(test_db_session)["days"] == []

    def test_what_was_sent_stays_in_the_log_after_the_payment(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, status="paid", amount_due=0.0))
        [day] = _log(test_db_session)["days"]
        assert day["sent"] == 1 and day["items"][0]["state_label"] == "Inviata"

    def test_a_reminder_not_sent_says_why(self, test_db_session):
        c = _cust(test_db_session, email=None)
        when = datetime(2026, 11, 17, 8, 15)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), status="skipped",
             skip_reason="senza_email", when=when, to_email=None)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK"), status="skipped",
             skip_reason="in_incasso", when=when, to_email=None)
        data = _log(test_db_session)
        [day] = data["days"]
        assert (day["sent"], day["missing"], day["stuck"]) == (0, 1, 0)
        assert sorted((e["state"], e["state_label"]) for e in day["items"]) == [
            ("closed", "Non partita: assegno in incasso"), ("missing", "Non partita: senza email")]
        assert data["totals"]["week_missing"] == 1 and data["stuck"] == []

    def test_a_second_notice_skipped_by_the_round_is_not_a_fact_of_a_day(self, test_db_session):
        """Il giro rivaluta ogni mattina le fatture saltate, senza datare il
        motivo: nel registro dei giorni non c'è (sta sotto il nome del cliente)."""
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv, when=datetime(2026, 11, 3, 8, 15))
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16))
        assert _log(test_db_session, cfg=_on())["days"] == []
        assert _labels(test_db_session, c, _on(), now=LATE)[1] == "2° avviso sospeso: in attesa"

    def test_a_refused_notice_later_skipped_by_the_round_keeps_its_day(self, test_db_session):
        """Rifiuto certo il 17/11 (non partita), poi il giro del 18 la salta:
        nel giorno del tentativo si dice l'una e l'altra cosa."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16),
             attempted_at=datetime(2026, 11, 17, 8, 16))
        [entry] = _log(test_db_session, cfg=_on())["days"][0]["items"]
        assert (entry["day"], entry["state"], entry["state_label"]) == (
            "2026-11-17", "closed", "Non riuscita, poi non partita: in attesa")

    def test_a_reminder_attempted_and_then_rewritten_as_skipped_is_uncertain(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="forse_pagata",
             when=datetime(2026, 11, 16, 8, 15), attempted_at=datetime(2026, 11, 16, 8, 15))
        data = _log(test_db_session)
        [entry] = data["days"][0]["items"]
        assert (entry["state"], entry["state_label"]) == ("missing", "Esito incerto: da verificare su Resend")
        assert data["stuck"] == []

    def test_a_stuck_email_is_in_its_day_and_in_the_to_do_list(self, test_db_session):
        c = _cust(test_db_session)
        for num in ("2026/00001804/SAK", "2026/00001812/SAK"):
            inv = _inv(test_db_session, c, num)
            _rem(test_db_session, inv, when=datetime(2026, 11, 3, 8, 15))
            _fol(test_db_session, inv, status="failed", error_kind="network",
                 error="Resend non raggiungibile: ReadTimeout", group="g1")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001820/SAK", due=TODAY), status="sending",
             when=datetime(2026, 11, 17, 8, 15))
        data = _log(test_db_session)
        assert [(d["date"], d["stuck"]) for d in data["days"]] == [("2026-11-18", 1), ("2026-11-17", 1)]
        assert [(e["kind"], e["state_label"], len(e["invoices"])) for e in data["stuck"]] == [
            ("followup", "Ferma: da gestire", 2), ("promemoria", "Ferma: da gestire", 1)]
        assert data["stuck"][0]["detail"] == "Resend non raggiungibile: ReadTimeout"
        # Una email ferma non è una email inviata, nemmeno nel conto di oggi.
        assert data["totals"] == {"today_sent": 0, "week_sent": 0, "week_missing": 0, "stuck": 2}
        # L'esito incerto resta dell'operatore anche a secondo avviso acceso.
        assert [e["state_label"] for e in _log(test_db_session, cfg=_on())["stuck"]] == ["Ferma: da gestire"] * 2

    def test_an_old_stuck_email_is_still_to_do(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 9, 1))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="provider", when=datetime(2026, 9, 8, 8, 16))
        data = _log(test_db_session)
        assert data["days"] == [] and [e["day"] for e in data["stuck"]] == ["2026-09-08"]

    def test_a_failure_on_a_settled_invoice_needs_nobody(self, test_db_session):
        c = _cust(test_db_session)
        _fol(test_db_session, _inv(test_db_session, c, status="paid", amount_due=0.0), status="failed",
             error_kind="network")
        data = _log(test_db_session)
        [entry] = data["days"][0]["items"]
        assert (entry["state"], entry["state_label"]) == ("closed", "Non conclusa: fattura non più da saldare")
        assert data["stuck"] == [] and data["days"][0]["stuck"] == 0

    def test_a_failure_the_round_retries_is_not_to_do(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 17, 8, 16))
        on = _log(test_db_session, cfg=_on())
        assert on["stuck"] == []
        retried = [e for d in on["days"] for e in d["items"] if e["kind"] == "followup"][0]
        assert (retried["state"], retried["state_label"]) == ("planned", "Non riuscita: il giro la ritenta")
        off = _log(test_db_session, cfg=_off())
        assert [e["state_label"] for e in off["stuck"]] == ["Ferma: da gestire"]

    @pytest.mark.parametrize("status,kind", [("failed", "network"), ("failed", "conflict"), ("sending", None)])
    def test_an_uncertain_failure_is_never_left_to_the_round(self, test_db_session, status, kind):
        """Anche con tutto il resto a posto (acceso, promemoria inviato, giorni
        utili): ciò che potrebbe essere arrivato lo riprende solo un operatore."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status=status, error_kind=kind, when=datetime(2026, 11, 17, 8, 16))
        data = _log(test_db_session, cfg=_on(), now=EARLY)
        assert [e["state_label"] for e in data["stuck"]] == ["Ferma: da gestire"]

    def test_a_failure_the_round_would_skip_is_not_promised(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 17, 8, 16))
        data = _log(test_db_session, cfg=_on())
        [entry] = [e for d in data["days"] for e in d["items"] if e["kind"] == "followup"]
        assert (entry["state"], entry["state_label"]) == ("paused", "Non riuscita, ora sospeso: in attesa")
        assert data["stuck"] == [] and data["totals"]["week_missing"] == 0
        c.recovery_status = "idle"
        c.email = None
        test_db_session.commit()
        [entry] = [e for d in _log(test_db_session, cfg=_on())["days"] for e in d["items"] if e["kind"] == "followup"]
        assert (entry["state"], entry["state_label"]) == ("missing", "Non riuscita, ora non previsto: senza email")

    def test_a_certain_failure_with_no_round_left_is_to_do(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 5))   # oggi è l'ultimo giorno utile
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 17, 8, 16))
        assert _log(test_db_session, cfg=_on(), now=EARLY)["stuck"] == []
        assert [e["state_label"] for e in _log(test_db_session, cfg=_on(), now=LATE)["stuck"]] == ["Ferma: da gestire"]

    @pytest.mark.parametrize("kind,state,label", [("pdf", "closed", "Non partita: invio non riuscito"),
                                                  ("network", "missing", "Esito incerto: da verificare su Resend")])
    @pytest.mark.parametrize("reminder", ["nessuno", "saltato", "in test", "altra scadenza", "altro cliente"])
    def test_without_the_reminder_a_failed_notice_is_not_to_do(self, test_db_session, kind, state, label, reminder):
        """«Riprova» la rifiuterebbe per sempre («promemoria non inviato»): vale
        solo il promemoria REALE, INVIATO, per QUESTA scadenza, a QUESTO cliente."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        early = datetime(2026, 11, 3, 8, 15)
        if reminder == "saltato":
            _rem(test_db_session, inv, status="skipped", skip_reason="senza_email", when=early)
        elif reminder == "in test":
            _rem(test_db_session, inv, mode="test", when=early)
        elif reminder == "altra scadenza":
            _rem(test_db_session, inv, due=date(2026, 11, 2), when=early)
        elif reminder == "altro cliente":
            other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
            _rem(test_db_session, inv, customer_id=other.id, when=early)
        _fol(test_db_session, inv, status="failed", error_kind=kind)
        data = _log(test_db_session, cfg=_on(), now=EARLY)
        assert data["stuck"] == []
        assert [(e["state"], e["state_label"]) for e in data["days"][0]["items"] if e["kind"] == "followup"] == [
            (state, label)]

    @pytest.mark.parametrize("status,kind", [("failed", "pdf"), ("failed", "validation"), ("failed", "network"),
                                             ("sending", None)])
    def test_a_failed_reminder_past_its_due_date_is_uncertain_and_not_to_do(self, test_db_session, status, kind):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status=status, error_kind=kind,
             when=datetime(2026, 11, 13, 8, 15))
        data = _log(test_db_session)
        [entry] = data["days"][0]["items"]
        assert (entry["state"], entry["state_label"]) == ("missing", "Esito incerto: da verificare su Resend")
        assert data["stuck"] == [] and data["totals"]["week_missing"] == 1

    def test_a_stuck_reminder_of_another_due_date_or_customer_is_not_to_do(self, test_db_session):
        """Scadenza cambiata dopo il tentativo, o fattura passata a un altro
        cliente: «Riprova» manderebbe un'altra cosa a un altro. Non la si invita."""
        c = _cust(test_db_session)
        moved = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 30))
        _rem(test_db_session, moved, due=date(2026, 11, 20), status="failed", error_kind="network",
             when=datetime(2026, 11, 17, 8, 15))
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        rematched = _inv(test_db_session, other, "2026/00001812/SAK", due=date(2026, 11, 20))
        _rem(test_db_session, rematched, status="failed", error_kind="network", when=datetime(2026, 11, 17, 8, 15),
             customer_id=c.id)
        data = _log(test_db_session)
        assert data["stuck"] == []
        assert [e["state_label"] for e in data["days"][0]["items"]] == ["Esito incerto: da verificare su Resend"] * 2

    @pytest.mark.parametrize("kind,state,label", [("network", "missing", "Esito incerto: da verificare su Resend"),
                                                  ("pdf", "closed", "Non partita: invio non riuscito")])
    def test_a_stuck_notice_that_riprova_would_refuse_is_not_to_do(self, test_db_session, kind, state, label):
        """Scadenza cambiata, o fattura passata a un altro cliente: «Riprova»
        rifiuta per sempre. Non deve restare tra le «da gestire»."""
        c = _cust(test_db_session)
        moved = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 10))
        _rem(test_db_session, moved)   # il promemoria per la scadenza di adesso c'è
        _fol(test_db_session, moved, due=date(2026, 11, 9), status="failed", error_kind=kind)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        rematched = _inv(test_db_session, other, "2026/00001812/SAK")
        _rem(test_db_session, rematched)   # e anche qui, al nuovo cliente
        _fol(test_db_session, rematched, status="failed", error_kind=kind, customer_id=c.id)
        data = _log(test_db_session, cfg=_on())
        assert data["stuck"] == [] and data["totals"]["stuck"] == 0
        assert [(e["state"], e["state_label"]) for e in data["days"][0]["items"] if e["kind"] == "followup"] == [
            (state, label)] * 2

    def test_the_range_is_whole_italian_days(self, test_db_session):
        c = _cust(test_db_session)
        # Le 23:30 UTC del 4/11 sono le 00:30 italiane del 5/11: primo giorno del periodo.
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), when=datetime(2026, 11, 4, 23, 30))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"), when=datetime(2026, 11, 4, 22, 30))
        assert [d["date"] for d in _log(test_db_session)["days"]] == ["2026-11-05"]

    def test_the_range_looks_at_what_is_shown_not_at_an_old_send_date(self, test_db_session):
        """Riga inviata in test un mese fa e poi fallita in reale ieri: è un
        fatto di ieri. E viceversa un vecchio tentativo non entra nel periodo
        per una data di invio rimasta scritta."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK", due=TODAY), status="failed",
             error_kind="network", when=datetime(2026, 11, 17, 8, 15), sent_at=datetime(2026, 10, 20, 8, 15))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK", due=TODAY), status="failed",
             error_kind="network", when=datetime(2026, 10, 20, 8, 15), sent_at=datetime(2026, 11, 17, 8, 15))
        data = _log(test_db_session)
        assert [(d["date"], len(d["items"])) for d in data["days"]] == [("2026-11-17", 1)]
        assert data["days"][0]["items"][0]["invoices"][0]["number"] == "2026/00001804/SAK"

    @pytest.mark.parametrize("asked,used", [(1, 7), (7, 7), (30, 30), (500, 90), (None, 7)])
    def test_the_range_is_between_a_week_and_ninety_days(self, test_db_session, asked, used):
        assert _log(test_db_session, days=asked)["range"]["days"] == used

    def test_the_week_counts_seven_days(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), when=datetime(2026, 11, 12, 8, 15))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"), when=datetime(2026, 11, 11, 8, 15))
        totals = _log(test_db_session)["totals"]
        assert (totals["today_sent"], totals["week_sent"]) == (0, 1)

    def test_the_link_goes_to_the_surviving_card(self, test_db_session):
        survivor = _cust(test_db_session, "Shabu Casatenovo")
        merged = _cust(test_db_session, "Shabu doppione", "x@shabu.it", partita_iva="01234567897",
                       merged_into=survivor.id)
        _rem(test_db_session, _inv(test_db_session, survivor), customer_id=merged.id)
        [entry] = _log(test_db_session)["days"][0]["items"]
        assert entry["customer_id"] == survivor.id and entry["ragione_sociale"] == "Shabu doppione"

    def test_a_row_whose_invoice_is_gone_does_not_break_the_log(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        row = _rem(test_db_session, inv)
        row.invoice_id = 999999
        test_db_session.commit()
        [entry] = _log(test_db_session)["days"][0]["items"]
        assert entry["invoices"][0]["number"] is None and entry["ragione_sociale"] == "Shabu Casatenovo"

    def test_a_row_without_customer_falls_back_to_the_name_on_the_invoice(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), customer_id=None)
        [entry] = _log(test_db_session)["days"][0]["items"]
        assert entry["customer_id"] is None and entry["ragione_sociale"] == "Shabu Casatenovo"

    @pytest.mark.parametrize("copies,shown", [
        (["a@x.it", "b@x.it"], ["a@x.it", "b@x.it"]), (None, []), ("a@x.it", []), ({"a@x.it": 1}, []), (5, []),
        (["a@x.it", None, 7, ""], ["a@x.it"]),
    ])
    def test_copies_written_in_an_odd_shape_never_break_the_page(self, test_db_session, copies, shown):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), cc_json=copies)
        assert _log(test_db_session)["days"][0]["items"][0]["cc"] == shown

    def test_an_unreadable_second_notice_registry_never_hides_the_reminders(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv)
        real = es._log_query
        rollbacks = []
        monkeypatch.setattr(test_db_session, "rollback", lambda: rollbacks.append(1))

        def broken(session, model):
            if model is InvoiceFollowupEmail:
                session.execute(text("select * from tabella_che_non_esiste"))
            return real(session, model)

        monkeypatch.setattr(es, "_log_query", broken)
        data = _log(test_db_session)
        assert data["followup_readable"] is False and rollbacks == [1]
        assert [e["kind"] for d in data["days"] for e in d["items"]] == ["promemoria"]

    def test_on_a_weekend(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 14))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 20, 8, 16),
             due=date(2026, 11, 14))
        saturday = _at(date(2026, 11, 21), 9)
        data = _log(test_db_session, cfg=_on(), now=saturday)
        assert data["today"] == "2026-11-21" and data["stuck"] == []
        assert data["days"][0]["items"][0]["state_label"] == "Non riuscita: il giro la ritenta"
        assert _labels(test_db_session, c, _on(), now=saturday)[1] == "2° avviso: nuovo tentativo 23/11"


# ── Costo e sola lettura ───────────────────────────────────────────

class TestCostAndReadOnly:
    def _seed(self, s, customers=25):
        for n in range(1, customers + 1):
            c = _cust(s, f"Cliente {n} Srl", f"c{n}@x.it", partita_iva=f"{n:011d}")
            twin = _cust(s, f"Doppione {n}", f"d{n}@x.it", partita_iva=f"9{n:010d}", merged_into=c.id)
            a = _inv(s, c, f"2026/000019{n:02d}/SAK", due=date(2026, 11, 3))
            b = _inv(s, c, f"2026/000029{n:02d}/SAK", due=date(2026, 11, 4))
            _rem(s, a, customer_id=twin.id)          # riga rimasta intestata alla scheda unita
            _rem(s, b)
            _fol(s, a, when=datetime(2026, 11, 10, 8, 16), customer_id=twin.id)
            _fol(s, b, status="failed", error_kind="network", when=datetime(2026, 11, 11, 8, 16))

    def _watch(self, engine):
        statements = []

        def before(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", before)
        return statements, lambda: event.remove(engine, "before_cursor_execute", before)

    def _seed_live(self, s, start, customers):
        """Clienti nei giorni utili: uno previsto, uno in attesa, uno con un
        rifiuto certo da ritentare, uno con una saltata scritta dal giro."""
        for n in range(start, start + customers):
            c = _cust(s, f"Vivo {n} Srl", f"v{n}@x.it", partita_iva=f"7{n:010d}",
                      recovery_status="waiting" if n % 4 == 1 else "idle")
            inv = _inv(s, c, f"2026/000039{n:02d}/SAK", due=DUE7)
            _rem(s, inv)
            if n % 4 == 2:
                _fol(s, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 17, 8, 16))
            if n % 4 == 3:
                _fol(s, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16))

    @pytest.mark.parametrize("cfg", [_off, _on])
    def test_the_number_of_queries_does_not_grow_with_the_customers(self, test_db_engine, test_db_session, cfg):
        """Clienti e Attività chiamano lo stato a ogni apertura: non deve
        costare una query per riga, né per cliente (schede unite, esclusioni
        e nomi accettati compresi), a secondo avviso spento o acceso."""
        self._seed(test_db_session, customers=4)
        self._seed_live(test_db_session, 1, 4)
        session = sessionmaker(bind=test_db_engine)()   # come in produzione (autoflush)
        statements, stop = self._watch(test_db_engine)
        try:
            data = customer_status(session, now_utc=EARLY, cfg=cfg())
            few = len(statements)
            assert len(data["customers"]) == 8
            del statements[:]
            sent_log(session, days=30, now_utc=EARLY, cfg=cfg())
            few_log = len(statements)
            session.close()
            self._seed(test_db_session, customers=24)
            self._seed_live(test_db_session, 5, 20)
            session = sessionmaker(bind=test_db_engine)()
            del statements[:]
            data = customer_status(session, now_utc=EARLY, cfg=cfg())
            assert len(data["customers"]) == 52
            assert len(statements) == few and few <= 8, (few, len(statements))
            del statements[:]
            log = sent_log(session, days=30, now_utc=EARLY, cfg=cfg())
            assert len(statements) == few_log and few_log <= 12, (few_log, len(statements))
            assert log["totals"]["stuck"] >= 28
        finally:
            stop()
            session.close()

    def test_the_email_body_is_never_loaded(self, test_db_engine, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), body_text="x" * 1000)
        session = sessionmaker(bind=test_db_engine)()
        statements, stop = self._watch(test_db_engine)
        try:
            customer_status(session, now_utc=LATE, cfg=_off())
            sent_log(session, now_utc=LATE, cfg=_off())
        finally:
            stop()
            session.close()
        assert statements and not any("body_text" in sql for sql in statements)

    def test_reading_only_reads(self, test_db_engine, test_db_session):
        """Nessuna INSERT / UPDATE / DELETE, nemmeno per un autoflush, con il
        secondo avviso acceso (previsioni ed esclusioni comprese)."""
        self._seed(test_db_session, customers=4)
        waiting = _cust(test_db_session, "In Attesa Srl", "w@x.it", partita_iva="00000000777", recovery_status="waiting")
        _rem(test_db_session, _inv(test_db_session, waiting, "2026/00001700/SAK"))
        session = sessionmaker(bind=test_db_engine)()
        statements, stop = self._watch(test_db_engine)
        try:
            customer_status(session, now_utc=EARLY, cfg=_on())
            sent_log(session, days=30, now_utc=EARLY, cfg=_on())
            assert not session.dirty and not session.new and not session.deleted
        finally:
            stop()
            session.close()
        assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements), statements


# ── API ────────────────────────────────────────────────────────────

class TestApi:
    def _real(self, monkeypatch, followup=False):
        from backend.config import config as real_config
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", True)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "re_x")
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_TEST_TO", "")
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_HOUR", 9)
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_MINUTE", 15)
        monkeypatch.setattr(real_config, "REMINDER_FOLLOWUP_ENABLED", followup)
        monkeypatch.setattr(real_config, "REMINDER_FOLLOWUP_DAYS", "7")
        monkeypatch.setattr(real_config, "REMINDER_FOLLOWUP_LOOKBACK_DAYS", "6")

    def test_customer_status(self, test_client, test_db_session, monkeypatch):
        self._real(monkeypatch, followup=True)
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        res = test_client.get("/api/reminders/email/customer-status")
        assert res.status_code == 200
        data = res.json()
        assert data["followup_mode"] == "reale" and data["today"] == "2026-11-18" and data["next_round"] == "2026-11-18"
        assert [it["label"] for it in data["customers"][str(c.id)]] == ["Promemoria 10/11", "2° avviso previsto 18/11"]
        # Lo stesso giorno, dopo il giro: la promessa passa a domani.
        CLOCK["now"] = LATE
        data = test_client.get("/api/reminders/email/customer-status").json()
        assert data["next_round"] == "2026-11-19"
        assert [it["label"] for it in data["customers"][str(c.id)]] == ["Promemoria 10/11", "2° avviso previsto 19/11"]

    def test_customer_status_with_the_second_notice_off(self, test_client, test_db_session, monkeypatch):
        self._real(monkeypatch)
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        data = test_client.get("/api/reminders/email/customer-status").json()
        assert data["followup_mode"] == "off"
        assert [it["label"] for it in data["customers"][str(c.id)]] == ["Promemoria 10/11"]

    def test_sent_log(self, test_client, test_db_session, monkeypatch):
        self._real(monkeypatch)
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        res = test_client.get("/api/reminders/email/sent", params={"days": 30})
        assert res.status_code == 200
        data = res.json()
        assert data["range"]["days"] == 30 and [d["date"] for d in data["days"]] == ["2026-11-10"]

    @pytest.mark.parametrize("days", [0, 6, 91])
    def test_sent_log_refuses_a_range_out_of_bounds(self, test_client, days):
        assert test_client.get("/api/reminders/email/sent", params={"days": days}).status_code == 422

    def test_the_new_routes_do_not_hide_the_row_detail(self, test_client, test_db_session):
        c = _cust(test_db_session)
        row = _rem(test_db_session, _inv(test_db_session, c))
        assert test_client.get(f"/api/reminders/email/{row.id}").status_code == 200
        assert test_client.get("/api/reminders/email/log").status_code == 200

    def test_sistema_always_lists_the_reminders_that_can_still_be_retried(self, test_client, test_db_session, monkeypatch):
        """«Da gestire in Sistema» non deve puntare a una riga che Sistema non
        elenca: il pannello mostra le ultime 30, ma un giro di fine mese ne
        scrive di più. I promemoria fermi ancora ritentabili ci sono sempre."""
        self._real(monkeypatch)
        s = test_db_session
        c = _cust(s)
        stuck = _rem(s, _inv(s, c, "2026/00001700/SAK", due=TODAY), status="failed", error_kind="network",
                     when=datetime(2026, 11, 17, 8, 15))
        past = _rem(s, _inv(s, c, "2026/00001701/SAK", due=date(2026, 11, 10)), status="failed", error_kind="network",
                    when=datetime(2026, 11, 9, 8, 15))
        for n in range(35):
            _rem(s, _inv(s, c, f"2026/000018{n:02d}/SAK", due=date(2026, 11, 19)),
                 when=datetime(2026, 11, 18, 8, 15) + timedelta(seconds=n))
        ids = [r["id"] for r in test_client.get("/api/reminders/email").json()["recent"]]
        assert len(ids) == 31 and len(set(ids)) == 31
        assert ids[0] == stuck.id and past.id not in ids   # a scadenza passata «Riprova» non la accetta
        # Se è già tra le ultime, non compare due volte.
        ids = [r["id"] for r in test_client.get("/api/reminders/email", params={"limit": 200}).json()["recent"]]
        assert ids.count(stuck.id) == 1 and len(ids) == 37

    def test_reading_writes_nothing(self, test_client, test_db_session, monkeypatch):
        self._real(monkeypatch, followup=True)
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        test_client.get("/api/reminders/email/customer-status")
        test_client.get("/api/reminders/email/sent")
        assert test_db_session.query(InvoiceFollowupEmail).count() == 0
        assert test_db_session.query(InvoiceReminderEmail).count() == 1
        assert not test_db_session.dirty and not test_db_session.new
