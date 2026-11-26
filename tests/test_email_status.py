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

from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail, SyncState
from backend.engine import email_status as es
from backend.engine import followup_email as fu
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
        attempted_at=moment if status in ("sent", "failed", "sending") else None,
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
        attempted_at=moment if status in ("sent", "failed", "sending") else None,
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
        (datetime(2026, 11, 18, 8, 15, tzinfo=timezone.utc), date(2026, 11, 19)),   # 09:15: il giro è partito
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

    def test_one_reminder_sent(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        [item] = _items(test_db_session, c)
        assert (item["kind"], item["state"], item["label"]) == ("promemoria", "sent", "Promemoria 10/11")
        assert item["date"] == "2026-11-10" and item["count"] == 1
        assert item["title"] == ("Fattura 2026/00001804/SAK: promemoria inviato il 10/11/2026 alle 09:15 "
                                 "a amministrazione@shabu.it")

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

    def test_a_reason_that_still_holds_and_is_a_choice_is_purple(self, test_db_session):
        c = _cust(test_db_session, recovery_status="lawyer")
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="avvocato")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK"), status="skipped", skip_reason="avvocato")
        assert _pairs(test_db_session, c) == [("paused", "2 promemoria non partiti: pratica dall'avvocato")]

    def test_an_excluded_customer(self, test_db_session):
        c = _cust(test_db_session, excluded=True)
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="cliente_escluso")
        assert _pairs(test_db_session, c) == [("paused", "Promemoria non partito: cliente escluso")]

    def test_a_reason_of_the_past_is_not_told_as_the_present(self, test_db_session):
        """Saltato perché «forse già pagata», poi la fattura è riapparsa: oggi
        non è «forse pagata», e non va letto così."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv, status="skipped", skip_reason="forse_pagata")
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("missing", "Promemoria non partito: allora forse già pagata")
        assert "oggi non più" in item["title"]
        inv.missing_streak = 1
        test_db_session.commit()
        assert _pairs(test_db_session, c) == [("paused", "Promemoria non partito: forse già pagata")]

    def test_an_email_added_later_does_not_rewrite_the_past(self, test_db_session):
        c = _cust(test_db_session)  # oggi l'email c'è
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="senza_email")
        assert _pairs(test_db_session, c) == [("missing", "Promemoria non partito: allora senza email")]

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
        assert "non si può più ritentare" in item["title"]

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
        assert _labels(test_db_session, c) == ["Promemoria 10/11", "Promemoria non partito: email non valida"]

    def test_the_reason_is_the_one_of_the_current_due_date(self, test_db_session):
        """Scadenza ANTICIPATA dopo un mancato invio: per la scadenza di adesso
        il promemoria è partito, il vecchio motivo non vale più."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=DUE7)
        _rem(test_db_session, inv, due=date(2026, 11, 20), status="skipped", skip_reason="senza_email",
             when=datetime(2026, 11, 3, 8, 15))
        _rem(test_db_session, inv)
        assert _labels(test_db_session, c) == ["Promemoria 10/11"]

    def test_a_problem_of_an_old_due_date_is_not_a_problem_of_the_new_one(self, test_db_session):
        """Scadenza spostata in avanti, non ancora valutata dal giro: ciò che è
        scritto per la vecchia data non descrive la nuova."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 30))
        _rem(test_db_session, inv, due=DUE7, status="skipped", skip_reason="senza_email")
        other = _inv(test_db_session, c, "2026/00001805/SAK", due=date(2026, 11, 30))
        _rem(test_db_session, other, due=DUE7, status="failed", error_kind="network")
        assert _items(test_db_session, c) == []

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
            ("promemoria", "paused", "Promemoria non partito: assegno in incasso"),
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
            _fol(test_db_session, _inv(test_db_session, c, num), status="sending", group="g1")
        [item] = _items(test_db_session, c)
        assert (item["label"], item["count"]) == ("2° avviso fermo 18/11", 1)
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
        """Ultimo giorno utile, giro già passato: nessun giro la riprenderà."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 5))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf")
        assert _labels(test_db_session, c, _on(), now=EARLY)[1] == "2° avviso: nuovo tentativo 18/11"
        assert _pairs(test_db_session, c, _on(), now=LATE)[1] == ("stuck", "2° avviso fermo 18/11")

    def test_a_certain_failure_out_of_the_window_is_for_the_operator(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 3))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 10, 8, 16))
        assert _labels(test_db_session, c, _on())[1] == "2° avviso fermo 10/11"

    def test_the_round_retries_only_what_it_still_looks_at(self, test_db_session):
        """Senza il promemoria per questa scadenza il giro non guarda la fattura."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _fol(test_db_session, inv, status="failed", error_kind="pdf")
        assert _pairs(test_db_session, c, _on()) == [("stuck", "2° avviso fermo 18/11")]

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

    def test_on_the_last_useful_day_after_the_round_nothing_is_promised(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 5)))
        assert _labels(test_db_session, c, _on(), now=EARLY) == ["Promemoria 04/11", "2° avviso previsto 18/11"]
        assert _labels(test_db_session, c, _on(), now=LATE) == ["Promemoria 04/11"]

    def test_a_wait_removed_too_late_is_not_turned_into_a_promise(self, test_db_session):
        """Ultimo giorno utile: alle 09:15 il giro scrive «saltata: in attesa»;
        alle 11:00 l'operatore toglie l'attesa. Nessun giro la manderà più."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 5))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 18, 8, 16))
        items = _items(test_db_session, c, _on(), now=LATE)
        assert [(it["state"], it["label"]) for it in items] == [
            ("sent", "Promemoria 04/11"), ("missing", "2° avviso non partito: allora in attesa")]
        assert "giorni utili per l'invio automatico sono passati" in items[1]["title"]
        # E i giorni dopo resta detto al passato, non come uno stato presente.
        tomorrow = _at(date(2026, 11, 19), 7)
        assert _labels(test_db_session, c, _on(), now=tomorrow)[1] == "2° avviso non partito: allora in attesa"

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

    def test_nothing_planned_once_the_window_has_passed(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=date(2026, 11, 4)))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 03/11"]

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
        # al presente perché oggi vale ancora…
        assert _pairs(test_db_session, c, _off())[1] == ("paused", "2° avviso non partito: in attesa")
        # …e al passato se non vale più.
        c.recovery_status = "idle"
        test_db_session.commit()
        assert _pairs(test_db_session, c, _off())[1] == ("missing", "2° avviso non partito: allora in attesa")

    def test_a_skip_out_of_the_window_is_final(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        a = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 2))
        b = _inv(test_db_session, c, "2026/00001812/SAK", due=date(2026, 11, 3))
        _fol(test_db_session, a, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 9, 8, 16))
        _fol(test_db_session, b, status="skipped", skip_reason="senza_email", when=datetime(2026, 11, 10, 8, 16))
        assert _pairs(test_db_session, c, _on()) == [
            ("missing", "2° avviso non partito: allora senza email"), ("paused", "2° avviso non partito: in attesa")]

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
        assert "scadenza poi cambiata" in items[2]["title"] and "potrebbe essere arrivato" in items[2]["title"]

    def test_a_skip_the_round_will_not_reconsider_keeps_its_written_reason(self, test_db_session):
        """Saltata nei giorni utili, ma senza il promemoria per questa scadenza:
        il giro non la riprende, quindi vale ciò che è scritto."""
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16))
        assert _pairs(test_db_session, c, _on()) == [("paused", "2° avviso non partito: in attesa")]

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
    def __init__(self):
        self.sent = []

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return f"msg-{len(self.sent)}"

    def close(self):
        pass


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
            round_time = _at(day, 8, 15)
            CLOCK["now"] = round_time
            mailer = _Mailer()
            fu.run_followups(s, now_utc=round_time, mailer=mailer, cfg=cfg)
            got = {by_email[m["to"][0]] for m in mailer.sent}
            assert got == promised, (day, sorted(got), sorted(promised))
            sent_total += len(mailer.sent)
            noon = _at(day, 11)
            CLOCK["now"] = noon
            after = customer_status(s, now_utc=noon, cfg=cfg)["customers"]
            still = [
                (cid, it["label"]) for cid, items in after.items() for it in items
                if it["state"] == "planned" and it["date"] == day.isoformat()
            ]
            assert still == [], (day, still)
            for cid in got:
                assert any(it["kind"] == "followup" and it["state"] == "sent" for it in after[cid]), (day, cid)
        # 16 clienti: fuori quello in attesa, quello senza email e quello che ha pagato.
        assert sent_total == 13


# ── Registro «Email inviate» ───────────────────────────────────────

def _log(s, days=14, cfg=None, now=LATE):
    return sent_log(s, days=days, now_utc=now, cfg=cfg or _off())


class TestSentLog:
    def test_empty(self, test_db_session):
        data = _log(test_db_session)
        assert data["days"] == [] and data["stuck"] == []
        assert data["totals"] == {"today_sent": 0, "week_sent": 0, "week_missing": 0, "stuck": 0}
        assert data["range"] == {"from": "2026-11-05", "to": "2026-11-18", "days": 14, "max_days": 90}
        assert data["followup_readable"] is True and data["today"] == "2026-11-18"

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

    def test_not_sent_rows_say_why(self, test_db_session):
        c = _cust(test_db_session, email=None)
        when = datetime(2026, 11, 17, 8, 15)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), status="skipped",
             skip_reason="senza_email", when=when, to_email=None)
        _fol(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"), status="skipped",
             skip_reason="in_attesa", when=when, to_email=None)
        data = _log(test_db_session)
        [day] = data["days"]
        assert (day["sent"], day["missing"], day["paused"], day["stuck"]) == (0, 1, 1, 0)
        assert sorted((e["state"], e["state_label"]) for e in day["items"]) == [
            ("missing", "Non partita: senza email"), ("paused", "Sospesa: in attesa")]
        assert data["totals"]["week_missing"] == 1 and data["stuck"] == []

    def test_a_stuck_email_is_in_its_day_and_in_the_to_do_list(self, test_db_session):
        c = _cust(test_db_session)
        for num in ("2026/00001804/SAK", "2026/00001812/SAK"):
            _fol(test_db_session, _inv(test_db_session, c, num), status="failed", error_kind="network",
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
        _fol(test_db_session, _inv(test_db_session, c, due=date(2026, 9, 1)), status="failed", error_kind="provider",
             when=datetime(2026, 9, 8, 8, 16))
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
        assert on["days"][0]["items"][0]["state_label"] == "Non riuscita: il giro ritenta da solo"
        off = _log(test_db_session, cfg=_off())
        assert [e["state_label"] for e in off["stuck"]] == ["Ferma: da gestire"]

    def test_a_failure_the_round_would_skip_is_not_promised(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 17, 8, 16))
        data = _log(test_db_session, cfg=_on())
        [entry] = [e for d in data["days"] for e in d["items"] if e["kind"] == "followup"]
        assert (entry["state"], entry["state_label"]) == ("paused", "Non riuscita, ora sospesa: in attesa")
        assert data["stuck"] == []
        c.recovery_status = "idle"
        c.email = None
        test_db_session.commit()
        [entry] = [e for d in _log(test_db_session, cfg=_on())["days"] for e in d["items"] if e["kind"] == "followup"]
        assert (entry["state"], entry["state_label"]) == ("missing", "Non riuscita, non ripartirà: senza email")

    def test_a_certain_failure_with_no_round_left_is_to_do(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 5))   # oggi è l'ultimo giorno utile
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf")
        assert _log(test_db_session, cfg=_on(), now=EARLY)["stuck"] == []
        assert [e["state_label"] for e in _log(test_db_session, cfg=_on(), now=LATE)["stuck"]] == ["Ferma: da gestire"]

    def test_a_certain_failure_without_the_reminder_is_to_do(self, test_db_session):
        c = _cust(test_db_session)
        _fol(test_db_session, _inv(test_db_session, c), status="failed", error_kind="pdf")
        assert [e["state_label"] for e in _log(test_db_session, cfg=_on(), now=EARLY)["stuck"]] == ["Ferma: da gestire"]

    @pytest.mark.parametrize("status,kind", [("failed", "pdf"), ("failed", "validation"), ("failed", "network"),
                                             ("sending", None)])
    def test_a_failed_reminder_past_its_due_date_is_uncertain_and_not_to_do(self, test_db_session, status, kind):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status=status, error_kind=kind,
             when=datetime(2026, 11, 13, 8, 15))
        data = _log(test_db_session)
        [entry] = data["days"][0]["items"]
        assert (entry["state"], entry["state_label"]) == ("missing", "Esito incerto: non più ritentabile")
        assert data["stuck"] == [] and data["totals"]["week_missing"] == 1

    @pytest.mark.parametrize("kind,label", [("network", "Esito incerto: non più ritentabile"),
                                           ("pdf", "Non partita: invio non riuscito")])
    def test_a_stuck_notice_that_riprova_would_refuse_is_not_to_do(self, test_db_session, kind, label):
        """Scadenza cambiata, o fattura passata a un altro cliente: «Riprova»
        rifiuta per sempre. Non deve restare tra le «da gestire»."""
        c = _cust(test_db_session)
        moved = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 12, 15))
        _fol(test_db_session, moved, due=date(2026, 11, 9), status="failed", error_kind=kind)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        rematched = _inv(test_db_session, other, "2026/00001812/SAK")
        _fol(test_db_session, rematched, status="failed", error_kind=kind, customer_id=c.id)
        data = _log(test_db_session, cfg=_on())
        assert data["stuck"] == [] and data["totals"]["stuck"] == 0
        assert [e["state_label"] for e in data["days"][0]["items"]] == [label, label]
        assert all(e["state"] == "missing" for e in data["days"][0]["items"])

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
        assert data["days"][0]["items"][0]["state_label"] == "Non riuscita: il giro ritenta da solo"
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

    def test_the_number_of_queries_does_not_grow_with_the_customers(self, test_db_engine, test_db_session):
        """Clienti e Attività chiamano lo stato a ogni apertura: non deve
        costare una query per riga (nemmeno per le schede unite)."""
        self._seed(test_db_session, customers=5)
        session = sessionmaker(bind=test_db_engine)()   # come in produzione (autoflush)
        statements, stop = self._watch(test_db_engine)
        try:
            data = customer_status(session, now_utc=LATE, cfg=_off())
            few = len(statements)
            assert len(data["customers"]) == 5 and all(len(items) == 3 for items in data["customers"].values())
            session.close()
            self._seed(test_db_session, customers=20)
            session = sessionmaker(bind=test_db_engine)()
            del statements[:]
            customer_status(session, now_utc=LATE, cfg=_off())
            assert len(statements) == few and few <= 4, statements
            del statements[:]
            log = sent_log(session, days=30, now_utc=LATE, cfg=_off())
            assert len(statements) <= 6 and log["totals"]["stuck"] == 25
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

    def test_reading_writes_nothing(self, test_client, test_db_session, monkeypatch):
        self._real(monkeypatch, followup=True)
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        test_client.get("/api/reminders/email/customer-status")
        test_client.get("/api/reminders/email/sent")
        assert test_db_session.query(InvoiceFollowupEmail).count() == 0
        assert test_db_session.query(InvoiceReminderEmail).count() == 1
        assert not test_db_session.dirty and not test_db_session.new
