"""Stato delle email automatiche accanto al nome del cliente e registro
«Email inviate» (backend/engine/email_status.py, API /api/reminders/email/
customer-status e /email/sent).

Richiesta owner 24–26/11/2026: la piattaforma deve dire, accanto al nome del
cliente, quali email sono partite e quando — per non sollecitare due volte e
per non perdere per strada chi non ha ricevuto nulla. Qui ogni regola di
lettura ha il suo test: solo invii reali, solo fatture ancora da saldare,
solo le email di QUEL cliente, mai una previsione a secondo avviso spento.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail
from backend.engine import email_status as es
from backend.engine import followup_email as fu
from backend.engine.email_status import customer_status, planned_day, sent_log

# Orologio FISSO: mercoledì 18/11/2026 alle 11:00 italiane (come nei test del
# secondo avviso). Scadenza 11/11 = scaduta da 7 giorni esatti.
FIXED_NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 18)
DUE7 = date(2026, 11, 11)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
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
        sent_at=moment if status == "sent" else None,
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


def _status(s, cfg=None):
    return customer_status(s, now_utc=FIXED_NOW, cfg=cfg or _off())


def _items(s, cust, cfg=None):
    return _status(s, cfg)["customers"].get(cust.id, [])


def _labels(s, cust, cfg=None):
    return [it["label"] for it in _items(s, cust, cfg)]


# ── Date e motivi ──────────────────────────────────────────────────

class TestRules:
    def test_planned_day_is_the_engine_window_seen_from_the_invoice(self):
        """Per ogni scadenza e ogni giorno: planned_day dà QUEL giorno se e
        solo se il giro di quel giorno ha la scadenza nella sua finestra."""
        cfg = _on()
        start = date(2026, 10, 20)
        for offset in range(60):
            day = start + timedelta(days=offset)
            window = fu.followup_window(datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc), cfg)
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
        failed = SimpleNamespace(status="failed", sent_at=None, attempted_at=datetime(2026, 11, 12, 9),
                                 created_at=datetime(2026, 11, 10))
        skipped = SimpleNamespace(status="skipped", sent_at=None, attempted_at=None, created_at=datetime(2026, 11, 10))
        assert es.event_time(sent) == datetime(2026, 11, 10, 8, 15)
        assert es.event_time(failed) == datetime(2026, 11, 12, 9)
        assert es.event_time(skipped) == datetime(2026, 11, 10)

    def test_event_day_is_the_italian_day(self):
        late = SimpleNamespace(status="sent", sent_at=datetime(2026, 11, 16, 23, 30), attempted_at=None, created_at=None)
        assert es.event_day(late) == date(2026, 11, 17)  # 23:30 UTC = 00:30 italiane del giorno dopo
        assert es.event_day(SimpleNamespace(status="skipped", sent_at=None, attempted_at=None, created_at=None)) is None


# ── Promemoria accanto al nome ─────────────────────────────────────

class TestReminderNextToTheName:
    def test_nothing_without_emails(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c)
        data = _status(test_db_session)
        assert data["customers"] == {} and data["followup_mode"] == "off" and data["followup_readable"] is True
        assert data["today"] == "2026-11-18"

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
        assert item["date"] is None

    def test_several_reminders_not_sent_for_the_same_reason(self, test_db_session):
        c = _cust(test_db_session, email=None)
        for num in ("2026/00001804/SAK", "2026/00001805/SAK"):
            _rem(test_db_session, _inv(test_db_session, c, num), status="skipped", skip_reason="senza_email")
        [item] = _items(test_db_session, c)
        assert item["label"] == "2 promemoria non partiti: senza email" and item["count"] == 2

    def test_reminder_held_by_a_choice_is_paused_not_missing(self, test_db_session):
        c = _cust(test_db_session, recovery_status="lawyer")
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="avvocato")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK"), status="skipped", skip_reason="avvocato")
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("paused", "2 promemoria sospesi: pratica dall'avvocato")

    def test_one_paused_reminder(self, test_db_session):
        c = _cust(test_db_session, excluded=True)
        _rem(test_db_session, _inv(test_db_session, c), status="skipped", skip_reason="cliente_escluso")
        assert _labels(test_db_session, c) == ["Promemoria sospeso: cliente escluso"]

    @pytest.mark.parametrize("status", ["failed", "sending"])
    @pytest.mark.parametrize("due", [TODAY, TODAY + timedelta(days=1)])
    def test_a_reminder_that_did_not_complete_can_be_retried_until_the_due_date(self, test_db_session, status, due):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, due=due), status=status, error_kind="network",
             error="ReadTimeout", when=datetime(2026, 11, 18, 8, 15))
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("stuck", "Promemoria fermo 18/11")
        assert "Sistema" in item["title"]

    @pytest.mark.parametrize("status,kind", [("failed", "network"), ("failed", "provider"), ("failed", None),
                                             ("failed", "other"), ("sending", None)])
    def test_after_the_due_date_an_uncertain_reminder_can_only_be_reported(self, test_db_session, status, kind):
        """«Riprova» non funziona più a scadenza passata: nessuno lo manderà,
        e non si sa se era arrivato. Va detto, non lasciato «da gestire»."""
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status=status, error_kind=kind)
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"]) == ("missing", "Promemoria dall'esito incerto 10/11")
        assert "non si può più ritentare" in item["title"] and "secondo avviso automatico non partirà" in item["title"]

    @pytest.mark.parametrize("kind", ["pdf", "validation"])
    def test_after_the_due_date_a_refused_reminder_was_not_sent(self, test_db_session, kind):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status="failed", error_kind=kind)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK"), status="failed", error_kind=kind)
        [item] = _items(test_db_session, c)
        assert (item["state"], item["label"], item["count"]) == (
            "missing", "2 promemoria non partiti: invio non riuscito", 2)

    def test_one_refused_reminder(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status="failed", error_kind="pdf")
        assert _labels(test_db_session, c) == ["Promemoria non partito: invio non riuscito"]

    def test_reminder_trouble(self):
        row = lambda **kw: SimpleNamespace(**dict(dict(status="failed", error_kind="pdf", due_date=DUE7), **kw))  # noqa: E731
        assert es.reminder_trouble(row(due_date=TODAY), TODAY) == es.RETRIABLE
        assert es.reminder_trouble(row(due_date=TODAY - timedelta(days=1)), TODAY) == es.LOST
        assert es.reminder_trouble(row(error_kind="validation"), TODAY) == es.LOST
        assert es.reminder_trouble(row(error_kind="network"), TODAY) == es.UNCERTAIN
        assert es.reminder_trouble(row(error_kind=None), TODAY) == es.UNCERTAIN
        assert es.reminder_trouble(row(status="sending"), TODAY) == es.UNCERTAIN
        assert es.reminder_trouble(row(due_date=None), TODAY) == es.LOST

    def test_due_date_moved_after_the_send(self, test_db_session):
        """Promemoria inviato per la vecchia scadenza, non partito per la nuova:
        si dicono tutti e due i fatti."""
        c = _cust(test_db_session)
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
        """Scadenza spostata in avanti, non ancora valutata dal giro: il motivo
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
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001806/SAK"), status="skipped", skip_reason="in_incasso")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001805/SAK"), status="skipped", skip_reason="scadenza_stimata")
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001807/SAK", due=date(2026, 11, 19)), status="failed",
             error_kind="provider")
        _rem(test_db_session, _inv(test_db_session, c))
        assert [(it["kind"], it["state"]) for it in _items(test_db_session, c)] == [
            ("promemoria", "sent"), ("promemoria", "stuck"), ("promemoria", "missing"), ("promemoria", "paused"),
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
        items = _items(test_db_session, c)
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
    def test_an_uncertain_send_is_stuck(self, test_db_session, cfg):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="network", error="ReadTimeout")
        items = _items(test_db_session, c, cfg())
        assert [(it["state"], it["label"]) for it in items] == [
            ("sent", "Promemoria 10/11"), ("stuck", "2° avviso fermo 18/11"),
        ]
        assert "Da gestire" in items[1]["title"]

    def test_a_stuck_email_with_two_invoices_counts_once(self, test_db_session):
        c = _cust(test_db_session)
        for num in ("2026/00001804/SAK", "2026/00001812/SAK"):
            _fol(test_db_session, _inv(test_db_session, c, num), status="sending", group="g1")
        [item] = _items(test_db_session, c)
        assert (item["label"], item["count"]) == ("2° avviso fermo 18/11", 1)

    def test_a_certain_failure_in_the_window_is_retried_by_the_round(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="pdf", error="PDF non generato")
        assert _labels(test_db_session, c, _on()) == ["Promemoria 10/11", "2° avviso: nuovo tentativo al prossimo giro"]
        assert _items(test_db_session, c, _on())[1]["state"] == "planned"
        # A secondo avviso spento nessun giro la riprende: è ferma.
        assert _labels(test_db_session, c, _off()) == ["Promemoria 10/11", "2° avviso fermo 18/11"]

    def test_a_certain_failure_out_of_the_window_is_stuck(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 3))
        _fol(test_db_session, inv, status="failed", error_kind="pdf", when=datetime(2026, 11, 10, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["2° avviso fermo 10/11"]

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
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 12))
        _rem(test_db_session, inv, status="skipped", skip_reason="senza_email")
        assert _labels(test_db_session, c, _on()) == ["Promemoria non partito: senza email"]

    def test_the_first_planned_day_of_the_customer(self, test_db_session):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 13)))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK", due=DUE7))
        item = _items(test_db_session, c, _on())[1]
        assert (item["label"], item["count"]) == ("2° avviso previsto 18/11", 2)
        assert "previsto il 20/11/2026" in item["title"] and "previsto il 18/11/2026" in item["title"]

    def test_a_waiting_case_is_paused_not_planned(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        _rem(test_db_session, _inv(test_db_session, c))
        items = _items(test_db_session, c, _on())
        assert [(it["state"], it["label"]) for it in items] == [
            ("sent", "Promemoria 10/11"), ("paused", "2° avviso sospeso: in attesa"),
        ]

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
        assert "non partirà" in items[1]["title"]

    def test_a_skip_inside_the_window_is_read_as_of_now(self, test_db_session):
        """Il giro rivaluta ogni mattina le righe saltate ancora in finestra:
        tolta l'attesa, il secondo avviso è di nuovo previsto."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 09/11", "2° avviso previsto 18/11"]
        c.recovery_status = "waiting"
        test_db_session.commit()
        assert _labels(test_db_session, c, _on()) == ["Promemoria 09/11", "2° avviso sospeso: in attesa"]
        # A secondo avviso spento non c'è nessun giro: vale ciò che è scritto.
        c.recovery_status = "idle"
        test_db_session.commit()
        assert _labels(test_db_session, c, _off()) == ["Promemoria 09/11", "2° avviso sospeso: in attesa"]

    def test_a_skip_out_of_the_window_is_final(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 2))
        b = _inv(test_db_session, c, "2026/00001812/SAK", due=date(2026, 11, 3))
        _fol(test_db_session, a, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 9, 8, 16))
        _fol(test_db_session, b, status="skipped", skip_reason="senza_email", when=datetime(2026, 11, 10, 8, 16))
        assert [(it["state"], it["label"]) for it in _items(test_db_session, c, _on())] == [
            ("missing", "2° avviso non partito: senza email"), ("paused", "2° avviso sospeso: in attesa"),
        ]

    def test_the_same_pause_is_said_once(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        old = _inv(test_db_session, c, "2026/00001804/SAK", due=date(2026, 11, 2))
        _fol(test_db_session, old, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 9, 8, 16))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"))
        assert _labels(test_db_session, c, _on()) == ["Promemoria 10/11", "2° avviso sospeso: in attesa"]

    def test_already_sent_is_not_planned_again(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv)
        assert _labels(test_db_session, c, _on()) == ["Promemoria 10/11", "2° avviso 18/11"]

    def test_stuck_is_not_planned_too(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, status="failed", error_kind="network")
        assert _labels(test_db_session, c, _on()) == ["Promemoria 10/11", "2° avviso fermo 18/11"]

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

    def test_a_stuck_notice_of_an_old_due_date_stays_to_do_and_the_new_one_is_planned(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _rem(test_db_session, inv)
        _fol(test_db_session, inv, due=date(2026, 10, 20), status="failed", error_kind="pdf",
             when=datetime(2026, 10, 27, 8, 16))
        assert _labels(test_db_session, c, _on()) == [
            "Promemoria 09/11", "2° avviso fermo 27/10", "2° avviso previsto 18/11"]

    def test_a_skip_the_round_will_not_reconsider_keeps_its_written_reason(self, test_db_session):
        """Saltata in finestra, ma senza il promemoria per questa scadenza: il
        giro non la riprende, quindi vale ciò che è scritto."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, due=date(2026, 11, 10))
        _fol(test_db_session, inv, status="skipped", skip_reason="in_attesa", when=datetime(2026, 11, 17, 8, 16))
        assert _labels(test_db_session, c, _on()) == ["2° avviso sospeso: in attesa"]

    def test_the_second_notice_of_another_customer_is_not_shown(self, test_db_session):
        old = _cust(test_db_session, "Vecchio Srl", "info@vecchio.it", partita_iva="01234567897")
        new = _cust(test_db_session, "Nuovo Srl", "info@nuovo.it")
        inv = _inv(test_db_session, new)
        _fol(test_db_session, inv, customer_id=old.id)
        assert _status(test_db_session)["customers"] == {}

    def test_an_unreadable_second_notice_registry_never_hides_the_reminders(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c))
        real = es._open_rows

        def broken(session, model):
            if model is InvoiceFollowupEmail:
                raise RuntimeError("tabella non leggibile")
            return real(session, model)

        monkeypatch.setattr(es, "_open_rows", broken)
        data = _status(test_db_session, _on())
        assert data["followup_readable"] is False
        assert [it["label"] for it in data["customers"][c.id]] == ["Promemoria 10/11", "2° avviso previsto 18/11"]


# ── Registro «Email inviate» ───────────────────────────────────────

def _log(s, days=14, cfg=None):
    return sent_log(s, days=days, now_utc=FIXED_NOW, cfg=cfg or _off())


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

    def test_a_group_is_dated_by_its_latest_attempt(self, test_db_session):
        c = _cust(test_db_session)
        _fol(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), group="g1", when=datetime(2026, 11, 18, 8, 16))
        _fol(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"), group="g1", when=datetime(2026, 11, 18, 8, 40))
        [entry] = _log(test_db_session)["days"][0]["items"]
        assert entry["time"] == "09:40" and len(entry["invoices"]) == 2

    @pytest.mark.parametrize("status,kind,label", [
        ("failed", "pdf", "Non partita: invio non riuscito"),
        ("failed", "network", "Esito incerto: non più ritentabile"),
        ("sending", None, "Esito incerto: non più ritentabile"),
    ])
    def test_a_failed_reminder_past_its_due_date_is_not_to_do(self, test_db_session, status, kind, label):
        c = _cust(test_db_session)
        _rem(test_db_session, _inv(test_db_session, c), status=status, error_kind=kind,
             when=datetime(2026, 11, 13, 8, 15))
        data = _log(test_db_session)
        [entry] = data["days"][0]["items"]
        assert (entry["state"], entry["state_label"]) == ("missing", label)
        assert data["stuck"] == [] and data["totals"]["week_missing"] == 1

    def test_a_stuck_notice_of_an_old_due_date_is_never_retried_by_the_round(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _fol(test_db_session, inv, due=date(2026, 11, 9), status="failed", error_kind="pdf")
        assert [e["state_label"] for e in _log(test_db_session, cfg=_on())["stuck"]] == ["Ferma: da gestire"]

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
        assert (entry["state"], entry["state_label"]) == ("closed", "Non riuscita: fattura non più da saldare")
        assert data["stuck"] == [] and data["days"][0]["stuck"] == 0

    def test_a_failure_the_round_retries_is_not_to_do(self, test_db_session):
        c = _cust(test_db_session)
        _fol(test_db_session, _inv(test_db_session, c), status="failed", error_kind="pdf")
        on = _log(test_db_session, cfg=_on())
        assert on["stuck"] == []
        assert on["days"][0]["items"][0]["state_label"] == "Non riuscita: il giro ritenta da solo"
        off = _log(test_db_session, cfg=_off())
        assert [e["state_label"] for e in off["stuck"]] == ["Ferma: da gestire"]

    def test_the_range_is_whole_italian_days(self, test_db_session):
        c = _cust(test_db_session)
        # Le 23:30 UTC del 4/11 sono le 00:30 italiane del 5/11: primo giorno del periodo.
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001804/SAK"), when=datetime(2026, 11, 4, 23, 30))
        _rem(test_db_session, _inv(test_db_session, c, "2026/00001812/SAK"), when=datetime(2026, 11, 4, 22, 30))
        assert [d["date"] for d in _log(test_db_session)["days"]] == ["2026-11-05"]

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

    def test_an_unreadable_second_notice_registry_never_hides_the_reminders(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c)
        _rem(test_db_session, inv)
        _fol(test_db_session, inv)
        real = es._log_query

        def broken(session, model):
            if model is InvoiceFollowupEmail:
                raise RuntimeError("tabella non leggibile")
            return real(session, model)

        monkeypatch.setattr(es, "_log_query", broken)
        data = _log(test_db_session)
        assert data["followup_readable"] is False
        assert [e["kind"] for d in data["days"] for e in d["items"]] == ["promemoria"]


# ── API ────────────────────────────────────────────────────────────

class TestApi:
    def _real(self, monkeypatch, followup=False):
        from backend.config import config as real_config
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", True)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "re_x")
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_TEST_TO", "")
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
        assert data["followup_mode"] == "reale" and data["today"] == "2026-11-18"
        assert [it["label"] for it in data["customers"][str(c.id)]] == ["Promemoria 10/11", "2° avviso previsto 18/11"]

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
