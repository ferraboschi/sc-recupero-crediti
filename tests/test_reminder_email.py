"""Promemoria email il giorno prima della scadenza (backend/engine/reminder_email.py,
backend/connectors/resend_mail.py, backend/api/reminders.py, job schedulato).

Regole owner 9/11/2026: una email per fattura in scadenza domani, con il PDF
della fattura e — se ci sono scadute non saldate — il PDF di riepilogo e il
totale nel testo. Modalità prova di default (compone, registra, NON invia).
"""
import base64
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.connectors.resend_mail import MailSendError, ResendMailer
from backend.database import Customer, Invoice, InvoiceReminderEmail
from backend.engine import reminder_email as re_mod
from backend.engine.reminder_email import (
    MODE_PROVA, MODE_REALE, MODE_TEST, compose, fmt_eur, overdue_invoices,
    preview, resolve_mode, run_reminders, skip_reason, target_due_date,
)

TOMORROW = date.today() + timedelta(days=1)


def _cfg(**over):
    base = dict(
        REMINDER_EMAIL_ENABLED=False, RESEND_API_KEY="", RESEND_API_URL="https://api.resend.test",
        REMINDER_EMAIL_FROM="Sake Company <fatture@sakecompany.com>",
        REMINDER_EMAIL_REPLY_TO="fatture@sakecompany.com", REMINDER_EMAIL_BCC="",
        REMINDER_EMAIL_TEST_TO="", REMINDER_EMAIL_HOUR=9, REMINDER_EMAIL_MINUTE=0,
        TIMEZONE="Europe/Rome",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _real_cfg(**over):
    return _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_test_key_SEGRETA", **over)


def _cust(s, nome="Shabu Casatenovo", email="amministrazione@shabu.it", **kw):
    c = Customer(ragione_sociale=nome, email=email, partita_iva=kw.pop("partita_iva", "12345670785"),
                 excluded=kw.pop("excluded", False), recovery_status=kw.pop("recovery_status", "idle"),
                 source="shopify", **kw)
    s.add(c)
    s.commit()
    return c


def _inv(s, cust, num, amount, due, status="open", days_overdue=0, **kw):
    i = Invoice(
        invoice_number=num, amount=amount, amount_due=kw.pop("amount_due", amount),
        issue_date=kw.pop("issue_date", due - timedelta(days=30)), due_date=due,
        days_overdue=days_overdue, customer_id=cust.id if cust else None,
        source_platform="fatturapro", status=status, **kw,
    )
    s.add(i)
    s.commit()
    return i


class FakeMailer:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times
        self.calls = 0

    def send(self, **kwargs):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MailSendError("Resend HTTP 422: invalid recipient")
        self.sent.append(kwargs)
        return f"msg-{self.calls}"


# ── Calendario, formati, modalità ─────────────────────────────────────

class TestBasics:
    def test_target_due_date_follows_italian_day(self):
        # 22:30 UTC del 9/11 = 23:30 a Roma → domani è il 10/11
        assert target_due_date(datetime(2026, 11, 9, 22, 30, tzinfo=timezone.utc)) == date(2026, 11, 10)
        # 23:30 UTC del 9/11 = 00:30 a Roma del 10/11 → domani è l'11/11
        assert target_due_date(datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)) == date(2026, 11, 11)

    def test_fmt_eur_italian(self):
        assert fmt_eur(1234.5) == "1.234,50 €"
        assert fmt_eur(0) == "0,00 €"
        assert fmt_eur(None) == "0,00 €"

    def test_resolve_mode(self):
        assert resolve_mode(_cfg()) == (MODE_PROVA, ["REMINDER_EMAIL_ENABLED", "RESEND_API_KEY"])
        assert resolve_mode(_cfg(REMINDER_EMAIL_ENABLED=True)) == (MODE_PROVA, ["RESEND_API_KEY"])
        assert resolve_mode(_real_cfg()) == (MODE_REALE, [])
        assert resolve_mode(_real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")) == (MODE_TEST, [])
        assert resolve_mode(_real_cfg(REMINDER_EMAIL_FROM=""))[0] == MODE_PROVA


# ── Composizione ──────────────────────────────────────────────────────

class TestCompose:
    def test_text_without_overdue(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "2026/00001861/SAK", 540.0, TOMORROW)
        msg = compose(c, inv, [])
        assert "2026/00001861/SAK" in msg["subject"] and "540,00 €" in msg["subject"]
        assert "Gentile Shabu Casatenovo" in msg["text"]
        assert "scade domani" in msg["text"]
        assert "scadute" not in msg["text"]  # niente paragrafo sulle scadute
        assert msg["overdue_count"] == 0 and msg["overdue_total"] == 0
        assert "<p>" in msg["html"] and "Shabu Casatenovo" in msg["html"]

    def test_text_with_overdue_total(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        o1 = _inv(test_db_session, c, "A/0", 1000.0, date.today() - timedelta(days=40), days_overdue=40)
        o2 = _inv(test_db_session, c, "A/00", 234.5, date.today() - timedelta(days=10), days_overdue=10)
        msg = compose(c, inv, [o1, o2])
        assert "2 fatture per un totale di 1.234,50 €" in msg["text"]
        assert "secondo allegato" in msg["text"]
        assert msg["overdue_count"] == 2 and msg["overdue_total"] == 1234.5

    def test_html_escapes_customer_name(self, test_db_session):
        c = _cust(test_db_session, nome="Bar <Tokyo> & Co")
        inv = _inv(test_db_session, c, "B/1", 10.0, TOMORROW)
        msg = compose(c, inv, [])
        assert "&lt;Tokyo&gt; &amp; Co" in msg["html"]


# ── Selezione ed esclusioni ───────────────────────────────────────────

class TestSelection:
    def test_only_invoices_due_tomorrow_unpaid(self, test_db_session):
        c = _cust(test_db_session)
        ok = _inv(test_db_session, c, "OK/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "PAID/1", 100.0, TOMORROW, status="paid")
        _inv(test_db_session, c, "VOID/1", 100.0, TOMORROW, status="void")
        _inv(test_db_session, c, "DISP/1", 100.0, TOMORROW, status="disputed")
        _inv(test_db_session, c, "ZERO/1", 100.0, TOMORROW, amount_due=0.0)
        _inv(test_db_session, c, "LATER/1", 100.0, TOMORROW + timedelta(days=1))
        _inv(test_db_session, None, "ORF/1", 100.0, TOMORROW)
        ids = [i.id for i in re_mod.candidate_invoices(test_db_session, TOMORROW)]
        assert ids == [ok.id]

    def test_skip_reasons(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "S/1", 100.0, TOMORROW)
        assert skip_reason(inv, None) == "senza_cliente"
        assert skip_reason(inv, c) is None
        c.email = "   "
        assert skip_reason(inv, c) == "senza_email"
        c.email = "x@y.it"
        c.recovery_status = "lawyer"
        assert skip_reason(inv, c) == "avvocato"
        c.recovery_status = "idle"
        c.excluded = True
        assert skip_reason(inv, c) == "cliente_escluso"
        c.excluded = False
        c.merged_into = 999
        assert skip_reason(inv, c) == "cliente_unito"
        c.merged_into = None
        inv.payment_pending = "assegno"
        assert skip_reason(inv, c) == "in_incasso"
        inv.bounced_at = datetime.utcnow()  # insoluto: torna da ricordare
        assert skip_reason(inv, c) is None

    def test_overdue_invoices_definition(self, test_db_session):
        c = _cust(test_db_session)
        target = _inv(test_db_session, c, "T/1", 100.0, TOMORROW)
        o1 = _inv(test_db_session, c, "O/1", 300.0, date.today() - timedelta(days=5), days_overdue=5)
        _inv(test_db_session, c, "O/paid", 300.0, date.today() - timedelta(days=5), days_overdue=5, status="paid")
        _inv(test_db_session, c, "O/disp", 300.0, date.today() - timedelta(days=5), days_overdue=5, status="disputed")
        _inv(test_db_session, c, "O/incasso", 300.0, date.today() - timedelta(days=5), days_overdue=5, payment_pending="assegno")
        _inv(test_db_session, c, "O/notdue", 300.0, date.today() + timedelta(days=5), days_overdue=0)
        other = _cust(test_db_session, nome="Altro", email="a@b.it", partita_iva="98765432100")
        _inv(test_db_session, other, "X/1", 300.0, date.today() - timedelta(days=5), days_overdue=5)
        got = overdue_invoices(test_db_session, c.id, exclude_id=target.id)
        assert [i.id for i in got] == [o1.id]


# ── Il giro: prova, reale, test, idempotenza, errori ──────────────────

class TestRun:
    def test_prova_prepares_without_sending(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "P/1", 540.0, TOMORROW)
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_cfg())
        assert summary["mode"] == MODE_PROVA
        assert summary["prepared"] == 1 and summary["sent"] == 0 and mailer.calls == 0
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "prepared" and row.invoice_id == inv.id and row.customer_id == c.id
        assert row.to_email == "amministrazione@shabu.it" and row.subject.startswith("Promemoria")
        assert row.attachments_json == ["fattura_2026_00001861_SAK.pdf"] or row.attachments_json == ["fattura_P_1.pdf"]
        assert row.body_text and "scade domani" in row.body_text

    def test_reale_sends_with_both_attachments_and_bcc(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "R/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "R/0", 1000.0, date.today() - timedelta(days=40), days_overdue=40)
        mailer = FakeMailer()
        cfg = _real_cfg(REMINDER_EMAIL_BCC="luigi@sakecompany.com")
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=cfg)
        assert summary["mode"] == MODE_REALE and summary["sent"] == 1 and summary["failed"] == 0
        sent = mailer.sent[0]
        assert sent["sender"] == "Sake Company <fatture@sakecompany.com>"
        assert sent["to"] == "amministrazione@shabu.it"
        assert sent["reply_to"] == "fatture@sakecompany.com" and sent["bcc"] == "luigi@sakecompany.com"
        names = [n for n, _ in sent["attachments"]]
        assert names == ["fattura_R_1.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        assert all(content[:4] == b"%PDF" for _, content in sent["attachments"])
        assert "1 fattura per un totale di 1.000,00 €" in sent["text"]
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "sent" and row.provider_message_id == "msg-1" and row.sent_at is not None
        assert row.overdue_count == 1 and row.overdue_total == 1000.0

    def test_test_mode_redirects_recipient_without_bcc(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "T/1", 100.0, TOMORROW)
        mailer = FakeMailer()
        cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com", REMINDER_EMAIL_BCC="luigi@sakecompany.com")
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=cfg)
        assert summary["mode"] == MODE_TEST and summary["sent"] == 1
        assert mailer.sent[0]["to"] == "lorenzo@example.com" and mailer.sent[0]["bcc"] is None
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.to_email == "lorenzo@example.com" and row.original_to == "amministrazione@shabu.it"
        assert row.mode == MODE_TEST

    def test_one_email_per_invoice(self, test_db_session):
        c = _cust(test_db_session)
        for n in range(5):
            _inv(test_db_session, c, f"M/{n}", 100.0 + n, TOMORROW)
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["sent"] == 5 and mailer.calls == 5
        assert len({s["subject"] for s in mailer.sent}) == 5

    def test_idempotent_and_retries_only_failed(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "I/1", 100.0, TOMORROW)
        failing = FakeMailer(fail_times=1)
        first = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert first["failed"] == 1
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "failed" and "Resend HTTP 422" in row.error
        second = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert second["sent"] == 1 and second["already"] == 0
        assert test_db_session.query(InvoiceReminderEmail).count() == 1
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "sent" and row.error is None
        third = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert third["already"] == 1 and third["sent"] == 0 and failing.calls == 2

    def test_skipped_rows_are_recorded_with_reason(self, test_db_session):
        c = _cust(test_db_session, email=None)
        _inv(test_db_session, c, "SK/1", 100.0, TOMORROW)
        lawyer = _cust(test_db_session, nome="Legale Srl", email="l@l.it", partita_iva="98765432100",
                       recovery_status="lawyer")
        _inv(test_db_session, lawyer, "SK/2", 100.0, TOMORROW)
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["skipped"] == 2 and summary["sent"] == 0 and mailer.calls == 0
        reasons = sorted(r.skip_reason for r in test_db_session.query(InvoiceReminderEmail).all())
        assert reasons == ["avvocato", "senza_email"]

    def test_pdf_failure_is_recorded_not_raised(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "PDF/1", 100.0, TOMORROW)
        monkeypatch.setattr(re_mod, "build_attachments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fpdf giù")))
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["failed"] == 1 and mailer.calls == 0
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "failed" and "fpdf giù" in row.error

    def test_preview_writes_nothing(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "PV/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "PV/0", 50.0, date.today() - timedelta(days=3), days_overdue=3)
        items = preview(test_db_session, TOMORROW)
        assert len(items) == 1 and items[0]["skip_reason"] is None
        assert items[0]["overdue_count"] == 1 and items[0]["overdue_total"] == 50.0
        assert "scade domani" in items[0]["text"]
        assert test_db_session.query(InvoiceReminderEmail).count() == 0


# ── Connettore Resend ─────────────────────────────────────────────────

class TestResendMailer:
    def test_payload_and_attachments(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "abc-123"})

        mailer = ResendMailer("re_key", "https://api.resend.test", transport=httpx.MockTransport(handler))
        mid = mailer.send(
            sender="Sake Company <fatture@sakecompany.com>", to="a@b.it", subject="S",
            text="corpo", html="<p>corpo</p>", reply_to="fatture@sakecompany.com",
            bcc="luigi@sakecompany.com, dario@sakecompany.com",
            attachments=[("fattura.pdf", b"%PDF-1.4 test")],
        )
        assert mid == "abc-123"
        assert seen["url"] == "https://api.resend.test/emails"
        assert seen["auth"] == "Bearer re_key"
        body = seen["body"]
        assert body["from"] == "Sake Company <fatture@sakecompany.com>" and body["to"] == ["a@b.it"]
        assert body["reply_to"] == ["fatture@sakecompany.com"]
        assert body["bcc"] == ["luigi@sakecompany.com", "dario@sakecompany.com"]
        assert body["attachments"] == [{"filename": "fattura.pdf", "content": base64.b64encode(b"%PDF-1.4 test").decode()}]
        assert body["html"] == "<p>corpo</p>" and body["text"] == "corpo"

    def test_error_message_has_status_and_no_key(self):
        def handler(request):
            return httpx.Response(422, json={"message": "Invalid `to` field", "name": "validation_error"})

        mailer = ResendMailer("re_SEGRETISSIMA", "https://api.resend.test", transport=httpx.MockTransport(handler))
        with pytest.raises(MailSendError) as exc:
            mailer.send(sender="x@sakecompany.com", to="bad", subject="S", text="t")
        assert "Resend HTTP 422" in str(exc.value) and "Invalid" in str(exc.value)
        assert "SEGRETISSIMA" not in str(exc.value)

    def test_network_error_is_wrapped(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        mailer = ResendMailer("re_key", "https://api.resend.test", transport=httpx.MockTransport(handler))
        with pytest.raises(MailSendError) as exc:
            mailer.send(sender="x@sakecompany.com", to="a@b.it", subject="S", text="t")
        assert "non raggiungibile" in str(exc.value)

    def test_missing_key_refused(self):
        with pytest.raises(MailSendError):
            ResendMailer("")


# ── API ───────────────────────────────────────────────────────────────

class TestApi:
    def test_status_preview_run_log_detail_attachment(self, test_client, test_db_session, monkeypatch):
        from backend.config import config as real_config
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", False)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "")
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "API/1", 540.0, TOMORROW)
        _inv(test_db_session, c, "API/0", 100.0, date.today() - timedelta(days=9), days_overdue=9)

        status = test_client.get("/api/reminders/email")
        assert status.status_code == 200
        data = status.json()
        assert data["mode"] == "prova" and "RESEND_API_KEY" in data["missing_config"]
        assert data["sender"] == "Sake Company <fatture@sakecompany.com>"
        assert data["target_due_date"] == TOMORROW.isoformat()
        assert data["preview"][0]["invoice_number"] == "API/1" and data["preview"][0]["overdue_count"] == 1
        assert data["recent"] == []

        run = test_client.post("/api/reminders/email/run")
        assert run.status_code == 200 and run.json()["prepared"] == 1

        log = test_client.get("/api/reminders/email/log", params={"customer_id": c.id})
        assert log.status_code == 200
        item = log.json()["items"][0]
        assert item["status"] == "prepared" and item["invoice_number"] == "API/1"
        assert item["ragione_sociale"] == "Shabu Casatenovo" and item["attachments"] == ["fattura_API_1.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        assert item["agents"] == []

        detail = test_client.get(f"/api/reminders/email/{item['id']}")
        assert detail.status_code == 200 and "scade domani" in detail.json()["body_text"]

        pdf = test_client.get(f"/api/reminders/email/{item['id']}/attachments/1")
        assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")
        assert pdf.content[:4] == b"%PDF"
        assert test_client.get(f"/api/reminders/email/{item['id']}/attachments/5").status_code == 404
        assert test_client.get("/api/reminders/email/999999").status_code == 404

        status2 = test_client.get("/api/reminders/email").json()
        assert status2["recent"][0]["invoice_id"] == inv.id and status2["preview"][0]["already"] == "prepared"


# ── Job schedulato ────────────────────────────────────────────────────

class TestSchedulerJob:
    def test_job_runs_engine_with_a_session_and_closes_it(self, monkeypatch):
        from backend import scheduler as sched

        class S:
            closed = False

            def close(self):
                self.closed = True

        sess = S()
        calls = []
        monkeypatch.setattr("backend.database.get_session_direct", lambda: sess)
        monkeypatch.setattr("backend.engine.reminder_email.run_reminders", lambda session: calls.append(session) or {"sent": 0, "items": []})
        out = sched.run_reminder_email_job()
        assert calls == [sess] and out == {"sent": 0, "items": []} and sess.closed

    def test_job_never_raises(self, monkeypatch):
        from backend import scheduler as sched
        monkeypatch.setattr("backend.database.get_session_direct", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("backend.engine.reminder_email.run_reminders", lambda session: (_ for _ in ()).throw(RuntimeError("db giù")))
        assert "error" in sched.run_reminder_email_job()
