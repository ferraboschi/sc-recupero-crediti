"""Avviso interno per le email automatiche rimaste ferme (backend/engine/stuck_alert.py).

Decisione owner 28/11/2026: nessuna verifica automatica su Resend; se
un'email resta ferma, un avviso a Lorenzo e a fatture@, una volta sola."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.connectors.resend_mail import MailSendError
from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail, SyncState
from backend.engine import reminder_email as rem
from backend.engine import stuck_alert as alert

NOW = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
TOMORROW = date(2026, 11, 11)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr("backend.engine.format.datetime", _Clock)
    monkeypatch.setattr("backend.engine.reminder_email.datetime", _Clock)


def _cfg(**over):
    base = dict(
        REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_test", RESEND_API_URL="https://api.resend.test",
        REMINDER_EMAIL_FROM="Sake Company <fatture@sakecompany.com>", REMINDER_EMAIL_REPLY_TO="fatture@sakecompany.com",
        REMINDER_EMAIL_BCC="", REMINDER_EMAIL_TEST_TO="", REMINDER_EMAIL_HOUR=9, REMINDER_EMAIL_MINUTE=15,
        REMINDER_EMAIL_CC="fatture@sakecompany.com", REMINDER_AGENT_EMAILS="", TIMEZONE="Europe/Rome",
        REMINDER_ALERT_TO="lorenzo@ef-ti.com, fatture@sakecompany.com",
    )
    base.update(over)
    return SimpleNamespace(**base)


class Mailer:
    def __init__(self, *errors):
        self.errors, self.sent = list(errors), []

    def send(self, **kw):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(kw)
        return f"msg-{len(self.sent)}"

    def close(self):
        pass


def _timeout():
    return MailSendError("Resend non raggiungibile: ReadTimeout", kind="network")


def _cust(s, nome="Shabu Casatenovo", email="amministrazione@shabu.it", piva="12345670785"):
    c = Customer(ragione_sociale=nome, email=email, partita_iva=piva, excluded=False, recovery_status="idle", source="shopify")
    s.add(c)
    s.commit()
    return c


def _inv(s, c, num, amount=100.0, due=TOMORROW):
    i = Invoice(invoice_number=num, amount=amount, amount_due=amount, issue_date=due - timedelta(days=30), due_date=due,
                due_date_source="real", days_overdue=0, customer_id=c.id, source_platform="fatturapro", status="open",
                customer_name_raw=c.ragione_sociale, customer_piva_raw=c.partita_iva)
    s.add(i)
    s.commit()
    return i


def _stuck_round(s):
    c = _cust(s)
    a = _inv(s, c, "2026/00001804/SAK - Fattura", 426.76)
    b = _inv(s, c, "2026/00001812/SAK - Fattura", 310.0)
    rem.run_reminders(s, due_date=TOMORROW, mailer=Mailer(_timeout()), cfg=_cfg())
    return c, a, b


def test_nothing_stuck_sends_nothing(test_db_session):
    c = _cust(test_db_session)
    _inv(test_db_session, c, "A/1")
    rem.run_reminders(test_db_session, due_date=TOMORROW, mailer=Mailer(), cfg=_cfg())
    mailer = Mailer()
    out = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    assert out == {"stuck": 0, "new": 0, "sent": False, "to": ["lorenzo@ef-ti.com", "fatture@sakecompany.com"], "skipped": None}
    assert mailer.sent == []


def test_one_alert_for_one_stuck_email_and_only_once(test_db_session):
    _stuck_round(test_db_session)
    mailer = Mailer()
    out = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    assert (out["stuck"], out["new"], out["sent"]) == (1, 1, True)
    [sent] = mailer.sent
    assert sent["to"] == ["lorenzo@ef-ti.com", "fatture@sakecompany.com"] and not sent.get("cc")
    assert sent["subject"] == "Recupero crediti: 1 email automatica ferma da verificare"
    text = sent["text"]
    assert "Promemoria — Shabu Casatenovo (amministrazione@shabu.it)" in text
    assert "fattura n. 2026/00001804/SAK, scadenza 11/11/2026, 426,76 €" in text and "2026/00001812/SAK" in text
    assert "Esito incerto: potrebbe essere arrivata" in text and "ReadTimeout" in text
    # Un secondo passaggio (il giorno dopo, un riavvio) non riavvisa.
    again = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    assert (again["stuck"], again["new"], again["sent"]) == (1, 0, False) and len(mailer.sent) == 1


def test_a_new_attempt_that_stays_stuck_is_a_new_alert(test_db_session, monkeypatch):
    _c, a, _b = _stuck_round(test_db_session)
    mailer = Mailer()
    alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    later = NOW + timedelta(hours=1)
    monkeypatch.setattr(_Clock, "now", classmethod(lambda cls, tz=None: later.astimezone(tz) if tz else later.replace(tzinfo=None)))
    monkeypatch.setattr(_Clock, "utcnow", classmethod(lambda cls: later.replace(tzinfo=None)))
    row = test_db_session.query(InvoiceReminderEmail).filter_by(invoice_id=a.id).one()
    already = MailSendError("Resend HTTP 409: Same idempotency key used with a different request payload", kind="validation")
    rem.retry_row(test_db_session, row, mailer=Mailer(already), cfg=_cfg())
    out = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=later)
    assert out["new"] == 1 and len(mailer.sent) == 2
    assert "con ogni probabilità è ARRIVATA" in mailer.sent[1]["text"]


def test_paid_invoices_and_non_real_rows_do_not_alert(test_db_session):
    _c, a, b = _stuck_round(test_db_session)
    for inv in (a, b):
        inv.status, inv.amount_due = "paid", 0
    other = _cust(test_db_session, "Altro Srl", "info@altro.it", "01234567897")
    x = _inv(test_db_session, other, "X/1")
    test_db_session.add(InvoiceReminderEmail(invoice_id=x.id, customer_id=other.id, due_date=x.due_date, status="failed",
                                             mode="test", error_kind="network"))
    test_db_session.commit()
    mailer = Mailer()
    out = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    assert out["stuck"] == 0 and mailer.sent == []


def test_the_second_notice_registry_is_watched_too(test_db_session):
    c = _cust(test_db_session)
    inv = _inv(test_db_session, c, "F/1", due=date(2026, 11, 3))
    test_db_session.add(InvoiceFollowupEmail(invoice_id=inv.id, customer_id=c.id, due_date=inv.due_date, status="failed",
                                             mode="reale", error_kind="validation", error="Resend HTTP 422: invalid",
                                             to_email="amministrazione@shabu.it", amount_due=100.0,
                                             attempted_at=datetime(2026, 11, 10, 8, 15)))
    test_db_session.commit()
    mailer = Mailer()
    out = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    assert out["sent"] and "Secondo avviso — Shabu Casatenovo" in mailer.sent[0]["text"]
    assert "Certamente NON partita" in mailer.sent[0]["text"]


def test_no_alert_outside_real_mode_or_without_recipients(test_db_session):
    _stuck_round(test_db_session)
    mailer = Mailer()
    test_mode = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(REMINDER_EMAIL_TEST_TO="x@y.it"), now_utc=NOW)
    nobody = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(REMINDER_ALERT_TO=" , non-valida"), now_utc=NOW)
    assert test_mode["skipped"] == "non in modalità reale" and nobody["skipped"].startswith("nessun destinatario")
    assert mailer.sent == [] and test_db_session.query(SyncState).filter_by(key=alert.STATE_KEY).first() is None


def test_a_failed_alert_never_raises_and_is_tried_again(test_db_session):
    _stuck_round(test_db_session)
    out = alert.send_stuck_alert(test_db_session, mailer=Mailer(_timeout()), cfg=_cfg(), now_utc=NOW)
    assert out["sent"] is False and out["skipped"] == "errore"
    mailer = Mailer()
    assert alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)["sent"] and len(mailer.sent) == 1


def test_the_alert_never_goes_to_a_customer_and_never_touches_the_registries(test_db_session):
    _c, a, _b = _stuck_round(test_db_session)
    before = [(r.id, r.status, r.error_kind, r.error, r.idempotency_key) for r in test_db_session.query(InvoiceReminderEmail)]
    mailer = Mailer()
    alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    test_db_session.expire_all()
    assert before == [(r.id, r.status, r.error_kind, r.error, r.idempotency_key) for r in test_db_session.query(InvoiceReminderEmail)]
    assert mailer.sent[0]["to"] == ["lorenzo@ef-ti.com", "fatture@sakecompany.com"] and a.id
    assert not mailer.sent[0].get("cc") and not mailer.sent[0].get("bcc")


def test_the_morning_job_runs_the_alert_after_the_two_rounds(monkeypatch):
    from backend import scheduler as sched
    order = []
    monkeypatch.setattr(sched, "run_reminder_email_job", lambda: order.append("reminder") or {"sent": 0})
    monkeypatch.setattr(sched, "run_followup_email_job", lambda: order.append("followup") or {"sent": 0})
    monkeypatch.setattr(sched, "run_stuck_alert_job", lambda: order.append("alert") or {"sent": False})
    out = sched.run_email_jobs()
    assert order == ["reminder", "followup", "alert"] and out["stuck_alert"] == {"sent": False}


def test_the_alert_job_never_raises(monkeypatch):
    from backend import scheduler as sched
    monkeypatch.setattr("backend.database.get_session_direct", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("backend.engine.stuck_alert.send_stuck_alert", lambda session: (_ for _ in ()).throw(RuntimeError("db giù")))
    assert sched.run_stuck_alert_job() == {"error": "db giù"}


def test_a_row_being_sent_right_now_is_not_stuck(test_db_session):
    c = _cust(test_db_session)
    inv = _inv(test_db_session, c, "S/1")
    row = InvoiceReminderEmail(invoice_id=inv.id, customer_id=c.id, due_date=inv.due_date, status="sending", mode="reale",
                               to_email="amministrazione@shabu.it", amount_due=100.0,
                               attempted_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None))
    test_db_session.add(row)
    test_db_session.commit()
    mailer = Mailer()
    assert alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)["stuck"] == 0
    later = NOW + timedelta(minutes=20)
    out = alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=later)
    assert out["sent"] and "Invio interrotto a metà" in mailer.sent[0]["text"]
    assert "Ultimo tentativo: 10/11/2026 10:59" in mailer.sent[0]["text"]   # ora italiana (09:59 UTC)


def test_a_round_that_retries_and_fails_the_same_way_does_not_alert_again(test_db_session):
    # Indirizzo rifiutato (422): il giro e ogni recupero al riavvio ritentano
    # da soli e falliscono uguale. Un solo avviso, non uno per giro.
    c = _cust(test_db_session)
    _inv(test_db_session, c, "V/1")
    rejected = lambda: MailSendError("Resend HTTP 422: invalid to", kind="validation")  # noqa: E731
    mailer = Mailer()
    for _ in range(4):
        rem.run_reminders(test_db_session, due_date=TOMORROW, mailer=Mailer(rejected()), cfg=_cfg())
        alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    assert len(mailer.sent) == 1 and "Certamente NON partita" in mailer.sent[0]["text"]


def test_marks_follow_what_is_still_stuck(test_db_session):
    _c, a, b = _stuck_round(test_db_session)
    mailer = Mailer()
    alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    kept = test_db_session.query(SyncState).filter_by(key=alert.STATE_KEY).one().result["marks"]
    assert len(kept) == 2
    # Saldate: non più ferme. Un'altra email ferma: l'elenco tiene solo lei.
    for inv in (a, b):
        inv.status, inv.amount_due = "paid", 0
    other = _cust(test_db_session, "Altro Srl", "info@altro.it", "01234567897")
    _inv(test_db_session, other, "X/1")
    rem.run_reminders(test_db_session, due_date=TOMORROW, mailer=Mailer(_timeout()), cfg=_cfg())
    alert.send_stuck_alert(test_db_session, mailer=mailer, cfg=_cfg(), now_utc=NOW)
    test_db_session.expire_all()
    assert len(test_db_session.query(SyncState).filter_by(key=alert.STATE_KEY).one().result["marks"]) == 1
    assert len(mailer.sent) == 2 and "Altro Srl" in mailer.sent[1]["text"] and "Shabu" not in mailer.sent[1]["text"]
