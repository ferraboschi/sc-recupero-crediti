"""Promemoria email il giorno prima della scadenza (backend/engine/reminder_email.py,
backend/engine/format.py, backend/connectors/resend_mail.py, backend/api/reminders.py,
job schedulato, PDF di backend/api/recovery.py).

Regole owner 9/11/2026: una email per fattura in scadenza domani, con la copia
di cortesia della fattura e — se ci sono scadute non saldate — il PDF di
riepilogo e il totale nel testo. Modalità prova di default (compone,
registra, NON invia). Nessun invio doppio: riga riservata prima dell'invio,
Idempotency-Key, ritento automatico solo per errori senza invio tentato.
"""
import base64
import json
import re
import zlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.connectors.resend_mail import MailSendError, ResendMailer
from backend.database import Customer, Invoice, InvoiceReminderEmail
from backend.engine import reminder_email as re_mod
from backend.engine.format import fmt_eur, fmt_eur_pdf, pdf_text, safe_filename
from backend.engine.reminder_email import (
    ERROR_NETWORK, ERROR_VALIDATION, MODE_PROVA, MODE_REALE, MODE_TEST, ModeMismatch, ReminderBusy,
    compose, in_catchup_window, needs_processing, normalize_email, overdue_invoices, preview,
    resolve_mode, retry_row, run_reminders, skip_reason, target_due_date,
)

TOMORROW = date.today() + timedelta(days=1)


def _cfg(**over):
    base = dict(
        REMINDER_EMAIL_ENABLED=False, RESEND_API_KEY="", RESEND_API_URL="https://api.resend.test",
        REMINDER_EMAIL_FROM="Sake Company <fatture@sakecompany.com>",
        REMINDER_EMAIL_REPLY_TO="fatture@sakecompany.com", REMINDER_EMAIL_BCC="",
        REMINDER_EMAIL_TEST_TO="", REMINDER_EMAIL_HOUR=9, REMINDER_EMAIL_MINUTE=15,
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
    """Fattura ben abbinata (stesso nome e P.IVA del cliente) con scadenza
    REALE: è il caso normale; i test delle esclusioni cambiano un campo."""
    i = Invoice(
        invoice_number=num, amount=amount, amount_due=kw.pop("amount_due", amount),
        issue_date=kw.pop("issue_date", due - timedelta(days=30)), due_date=due,
        due_date_source=kw.pop("due_date_source", "real"),
        days_overdue=days_overdue, customer_id=cust.id if cust else None,
        source_platform="fatturapro", status=status,
        customer_name_raw=kw.pop("customer_name_raw", cust.ragione_sociale if cust else None),
        customer_piva_raw=kw.pop("customer_piva_raw", cust.partita_iva if cust else None),
        **kw,
    )
    s.add(i)
    s.commit()
    return i


class FakeMailer:
    def __init__(self, fail_times=0, kind=ERROR_VALIDATION):
        self.sent = []
        self.fail_times = fail_times
        self.kind = kind
        self.calls = 0
        self.closed = False

    def send(self, **kwargs):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MailSendError("Resend HTTP 422: invalid recipient" if self.kind == ERROR_VALIDATION
                                else "Resend non raggiungibile: ReadTimeout", kind=self.kind)
        self.sent.append(kwargs)
        return f"msg-{self.calls}"

    def close(self):
        self.closed = True


def _pdf_strings(pdf_bytes: bytes) -> str:
    """Testo grezzo degli stream del PDF (fpdf2 li comprime con zlib)."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.S):
        chunk = m.group(1)
        try:
            out.append(zlib.decompress(chunk).decode("latin-1"))
        except Exception:
            out.append(chunk.decode("latin-1", "replace"))
    return "\n".join(out)


# ── Calendario, formati, modalità ─────────────────────────────────────

class TestBasics:
    def test_target_due_date_follows_italian_day(self):
        assert target_due_date(datetime(2026, 11, 9, 22, 30, tzinfo=timezone.utc)) == date(2026, 11, 10)
        assert target_due_date(datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)) == date(2026, 11, 11)

    def test_catchup_window_is_italian_office_hours(self):
        assert in_catchup_window(datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)) is True   # 09:30 Roma
        assert in_catchup_window(datetime(2026, 11, 9, 7, 30, tzinfo=timezone.utc)) is False  # 08:30 Roma
        assert in_catchup_window(datetime(2026, 11, 9, 17, 30, tzinfo=timezone.utc)) is False  # 18:30 Roma

    def test_fmt_eur_italian(self):
        assert fmt_eur(1234.5) == "1.234,50 €"
        assert fmt_eur(0) == "0,00 €"
        assert fmt_eur(None) == "0,00 €"
        assert fmt_eur_pdf(1234.5) == "1.234,50 EUR"

    def test_pdf_text_is_latin1_safe(self):
        assert pdf_text("L’Angolo del Sake – Caffè") == "L'Angolo del Sake - Caffè"
        assert pdf_text("Ristorante 日本 Srl") == "Ristorante ?? Srl"
        assert pdf_text("Domò Milano").encode("latin-1")  # non solleva
        assert pdf_text(None) == ""

    def test_safe_filename_transliterates(self):
        assert safe_filename("Domò Milano s.r.l.") == "Domo_Milano_s.r.l."
        assert safe_filename("2026/00001861/SAK") == "2026_00001861_SAK"
        assert safe_filename("日本") == "documento"

    def test_normalize_email(self):
        assert normalize_email(" Geron.Hanxhari@Cavo.net ") == "geron.hanxhari@cavo.net"
        assert normalize_email("info@x.it; amm@x.it") is None
        assert normalize_email("n/d") is None
        assert normalize_email("mario@x.it (ufficio)") is None
        assert normalize_email("") is None

    def test_resolve_mode(self):
        assert resolve_mode(_cfg()) == (MODE_PROVA, ["REMINDER_EMAIL_ENABLED", "RESEND_API_KEY"])
        assert resolve_mode(_cfg(REMINDER_EMAIL_ENABLED=True)) == (MODE_PROVA, ["RESEND_API_KEY"])
        assert resolve_mode(_real_cfg()) == (MODE_REALE, [])
        assert resolve_mode(_real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")) == (MODE_TEST, [])
        assert resolve_mode(_real_cfg(REMINDER_EMAIL_FROM=""))[0] == MODE_PROVA


# ── Composizione e PDF ────────────────────────────────────────────────

class TestCompose:
    def test_text_without_overdue(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "2026/00001861/SAK", 540.0, TOMORROW)
        msg = compose(c, inv, [])
        assert msg["subject"].startswith("Promemoria di cortesia: fattura n. 2026/00001861/SAK")
        assert "540,00 €" in msg["subject"]
        assert "Spett.le Shabu Casatenovo" in msg["text"]
        assert "Le ricordiamo" in msg["text"] and "scade domani" in msg["text"]
        assert "copia di cortesia" in msg["text"] and "Sistema di Interscambio" in msg["text"]
        assert "scadute" not in msg["text"]  # niente paragrafo sulle scadute
        assert "non tenere conto del presente promemoria" in msg["text"]
        assert "fatture@sakecompany.com" in msg["text"]
        assert msg["overdue_count"] == 0 and msg["overdue_total"] == 0
        assert "<p>" in msg["html"] and "Shabu Casatenovo" in msg["html"]

    def test_text_with_overdue_total_and_plural(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        o1 = _inv(test_db_session, c, "A/0", 1000.0, date.today() - timedelta(days=40), days_overdue=40)
        o2 = _inv(test_db_session, c, "A/00", 234.5, date.today() - timedelta(days=10), days_overdue=10)
        msg = compose(c, inv, [o1, o2])
        assert "risultano scadute e non ancora saldate 2 fatture, per un totale di 1.234,50 €" in msg["text"]
        assert "secondo allegato" in msg["text"]
        assert msg["overdue_count"] == 2 and msg["overdue_total"] == 1234.5
        one = compose(c, inv, [o1])
        assert "risulta scaduta e non ancora saldata 1 fattura, per 1.000,00 €" in one["text"]

    def test_html_escapes_customer_name(self, test_db_session):
        c = _cust(test_db_session, nome="Bar <Tokyo> & Co")
        inv = _inv(test_db_session, c, "B/1", 10.0, TOMORROW)
        assert "&lt;Tokyo&gt; &amp; Co" in compose(c, inv, [])["html"]

    def test_courtesy_pdf_has_no_internal_data_and_config_header(self, test_db_session, monkeypatch):
        from backend.api.recovery import _build_invoice_pdf, _build_riepilogativo_pdf
        from backend.config import config as real_config
        c = _cust(test_db_session, nome="L’Angolo del Sake – 日本")
        inv = _inv(test_db_session, c, "C/1", 1234.5, TOMORROW, status="contacted")
        o1 = _inv(test_db_session, c, "C/0", 1000.0, date.today() - timedelta(days=40), days_overdue=40)

        monkeypatch.setattr(real_config, "COMPANY_VAT", "")
        monkeypatch.setattr(real_config, "COMPANY_ADDRESS", "")
        text = _pdf_strings(bytes(_build_invoice_pdf(c, inv, courtesy=True)))
        assert "COPIA DI CORTESIA" in text and "non valida ai fini fiscali" in text
        assert "04aborita6" not in text and "P.IVA: " not in text.split("Destinatario")[0]
        assert "Contattato" not in text and "Fonte:" not in text and "SC Recupero Crediti" not in text
        assert "1.234,50 EUR" in text  # formato italiano, non 1.234.50
        assert "L'Angolo del Sake - ??" in text  # latin-1 safe, nessuna eccezione

        monkeypatch.setattr(real_config, "COMPANY_VAT", "01234567890")
        monkeypatch.setattr(real_config, "COMPANY_ADDRESS", "Via Esempio 1, Milano")
        text = _pdf_strings(bytes(_build_invoice_pdf(c, inv, courtesy=True)))
        assert "P.IVA: 01234567890 | Via Esempio 1, Milano" in text
        staff = _pdf_strings(bytes(_build_invoice_pdf(c, inv)))
        assert "FATTURA" in staff and "Contattato" in staff and "SC Recupero Crediti" in staff

        riep = _pdf_strings(bytes(_build_riepilogativo_pdf(c, [o1])))
        assert "1.000,00 EUR" in riep and "La preghiamo" in riep and "P.IVA: 01234567890" in riep


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
        assert [i.id for i in re_mod.candidate_invoices(test_db_session, TOMORROW)] == [ok.id]

    def test_skip_reasons(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "S/1", 100.0, TOMORROW)
        assert skip_reason(inv, None) == "senza_cliente"
        assert skip_reason(inv, c) is None
        c.email = "   "
        assert skip_reason(inv, c) == "senza_email"
        c.email = "info@x.it; amm@x.it"
        assert skip_reason(inv, c) == "email_non_valida"
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
        inv.due_date_source = "assumed"
        assert skip_reason(inv, c) == "scadenza_stimata"
        inv.due_date_source = None
        assert skip_reason(inv, c) == "scadenza_stimata"
        inv.due_date_source = "real"
        inv.missing_streak = 1
        assert skip_reason(inv, c) == "forse_pagata"
        inv.missing_streak = 0
        assert skip_reason(inv, c) is None

    def test_unverified_match_is_skipped_until_reviewed(self, test_db_session):
        c = _cust(test_db_session, nome="Sakura Sushi srl")
        inv = _inv(test_db_session, c, "V/1", 100.0, TOMORROW,
                   customer_name_raw="Ristorante Da Rossi", customer_piva_raw="98765432100")
        assert skip_reason(inv, c) == "abbinamento_da_verificare"
        inv.audit_reviewed_at = datetime.utcnow()  # verificata a mano in Sistema
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
        assert [i.id for i in overdue_invoices(test_db_session, c.id, exclude_id=target.id)] == [o1.id]


# ── Decisioni per riga ────────────────────────────────────────────────

class TestNeedsProcessing:
    def test_matrix(self):
        def R(**kw):
            return SimpleNamespace(**{"status": None, "mode": None, "error_kind": None, **kw})
        assert needs_processing(None, MODE_PROVA)[0] is True
        assert needs_processing(R(status="skipped"), MODE_PROVA)[0] is True
        assert needs_processing(R(status="failed", error_kind="validation"), MODE_REALE)[0] is True
        assert needs_processing(R(status="failed", error_kind="pdf"), MODE_REALE)[0] is True
        assert needs_processing(R(status="failed", error_kind="network"), MODE_REALE)[0] is False
        assert needs_processing(R(status="failed", error_kind=None), MODE_REALE)[0] is False
        assert needs_processing(R(status="prepared", mode="prova"), MODE_PROVA)[0] is False
        assert needs_processing(R(status="prepared", mode="prova"), MODE_TEST)[0] is True
        assert needs_processing(R(status="prepared", mode="prova"), MODE_REALE)[0] is True
        assert needs_processing(R(status="sent", mode="test"), MODE_REALE)[0] is True
        assert needs_processing(R(status="sent", mode="test"), MODE_TEST)[0] is False
        assert needs_processing(R(status="sent", mode="reale"), MODE_REALE)[0] is False
        assert needs_processing(R(status="sending", mode="reale"), MODE_REALE)[0] is False


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
        assert row.to_email == "amministrazione@shabu.it" and row.subject.startswith("Promemoria di cortesia")
        assert row.attachments_json == ["copia_cortesia_fattura_P_1.pdf"]
        assert row.body_text and "scade domani" in row.body_text
        assert row.attempted_at is not None

    def test_reale_sends_with_both_attachments_bcc_and_idempotency(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "R/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "R/0", 1000.0, date.today() - timedelta(days=40), days_overdue=40)
        mailer = FakeMailer()
        cfg = _real_cfg(REMINDER_EMAIL_BCC="luigi@sakecompany.com")
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=cfg)
        assert summary["mode"] == MODE_REALE and summary["sent"] == 1 and summary["failed"] == 0
        sent = mailer.sent[0]
        assert sent["sender"] == "Sake Company <fatture@sakecompany.com>"
        assert sent["to"] == ["amministrazione@shabu.it"]
        assert sent["reply_to"] == "fatture@sakecompany.com" and sent["bcc"] == "luigi@sakecompany.com"
        assert sent["idempotency_key"] == f"reminder-{inv.id}-{TOMORROW.isoformat()}-reale"
        names = [n for n, _ in sent["attachments"]]
        assert names == ["copia_cortesia_fattura_R_1.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        assert all(content[:4] == b"%PDF" for _, content in sent["attachments"])
        assert "1 fattura, per 1.000,00 €" in sent["text"]
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "sent" and row.provider_message_id == "msg-1" and row.sent_at is not None
        assert row.overdue_count == 1 and row.overdue_total == 1000.0
        assert mailer.closed is False  # mailer passato dall'esterno: non lo chiudiamo noi

    def test_test_mode_redirects_recipient_without_bcc(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "T/1", 100.0, TOMORROW)
        mailer = FakeMailer()
        cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com", REMINDER_EMAIL_BCC="luigi@sakecompany.com")
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=cfg)
        assert summary["mode"] == MODE_TEST and summary["sent"] == 1
        assert mailer.sent[0]["to"] == ["lorenzo@example.com"] and mailer.sent[0]["bcc"] is None
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.to_email == "lorenzo@example.com" and row.original_to == "amministrazione@shabu.it"
        assert row.mode == MODE_TEST

    def test_email_is_normalized_before_sending(self, test_db_session):
        c = _cust(test_db_session, email="  Amministrazione@Shabu.IT ")
        _inv(test_db_session, c, "N/1", 100.0, TOMORROW)
        mailer = FakeMailer()
        run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert mailer.sent[0]["to"] == ["amministrazione@shabu.it"]

    def test_one_email_per_invoice(self, test_db_session):
        c = _cust(test_db_session)
        for n in range(5):
            _inv(test_db_session, c, f"M/{n}", 100.0 + n, TOMORROW)
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["sent"] == 5 and mailer.calls == 5
        assert len({s["subject"] for s in mailer.sent}) == 5

    def test_validation_failure_is_retried_automatically(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "I/1", 100.0, TOMORROW)
        failing = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        first = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert first["failed"] == 1
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "failed" and row.error_kind == "validation" and "Resend HTTP 422" in row.error
        second = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert second["sent"] == 1 and second["already"] == 0
        assert test_db_session.query(InvoiceReminderEmail).count() == 1
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "sent" and row.error is None and row.error_kind is None
        third = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert third["already"] == 1 and third["sent"] == 0 and failing.calls == 2

    def test_network_failure_is_not_retried_automatically_but_manually(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "NET/1", 100.0, TOMORROW)
        failing = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "failed" and row.error_kind == "network"
        again = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert again["already"] == 1 and failing.calls == 1  # nessun rinvio automatico
        manual = retry_row(test_db_session, row, mailer=failing, cfg=_real_cfg())
        assert manual["sent"] == 1 and failing.calls == 2
        assert test_db_session.query(InvoiceReminderEmail).one().status == "sent"

    def test_retry_refuses_rows_not_stuck_or_invoices_paid(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "RR/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=FakeMailer(), cfg=_real_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        with pytest.raises(ValueError):
            retry_row(test_db_session, row, mailer=FakeMailer(), cfg=_real_cfg())
        row.status = "failed"
        row.error_kind = "network"
        inv.status = "paid"
        test_db_session.commit()
        with pytest.raises(ValueError):
            retry_row(test_db_session, row, mailer=FakeMailer(), cfg=_real_cfg())

    def test_prova_then_real_same_day_sends_for_real(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "PR/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=FakeMailer(), cfg=_cfg())
        assert test_db_session.query(InvoiceReminderEmail).one().status == "prepared"
        mailer = FakeMailer()
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=test_cfg)["sent"] == 1
        assert mailer.sent[0]["to"] == ["lorenzo@example.com"]
        real = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert real["sent"] == 1 and mailer.sent[1]["to"] == ["amministrazione@shabu.it"]
        assert mailer.sent[1]["idempotency_key"].endswith("-reale")
        assert test_db_session.query(InvoiceReminderEmail).count() == 1
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())["already"] == 1

    def test_reservation_race_counts_as_already(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "RACE/1", 100.0, TOMORROW)
        monkeypatch.setattr(re_mod, "_reserve", lambda session, row: False)
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["already"] == 1 and summary["sent"] == 0 and mailer.calls == 0

    def test_stuck_sending_row_waits_for_manual_retry(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "ST/1", 100.0, TOMORROW)
        test_db_session.add(InvoiceReminderEmail(invoice_id=inv.id, customer_id=c.id, due_date=TOMORROW,
                                                 status="sending", mode="reale"))
        test_db_session.commit()
        mailer = FakeMailer()
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())["already"] == 1
        assert mailer.calls == 0
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert retry_row(test_db_session, row, mailer=mailer, cfg=_real_cfg())["sent"] == 1

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
        # Il giorno stesso il cliente riceve l'email → al giro dopo parte.
        c.email = "nuova@shabu.it"
        test_db_session.commit()
        again = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert again["sent"] == 1 and mailer.sent[0]["to"] == ["nuova@shabu.it"]

    def test_pdf_failure_is_recorded_not_raised(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "PDF/1", 100.0, TOMORROW)
        monkeypatch.setattr(re_mod, "build_attachments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fpdf giù")))
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["failed"] == 1 and mailer.calls == 0
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "failed" and row.error_kind == "pdf" and "fpdf giù" in row.error

    def test_unexpected_error_never_stops_the_round(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        bad = _inv(test_db_session, c, "BAD/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "GOOD/1", 100.0, TOMORROW)
        original = re_mod.compose

        def exploding(customer, invoice, overdue):
            if invoice.invoice_number == "BAD/1":
                raise RuntimeError("boom")
            return original(customer, invoice, overdue)

        monkeypatch.setattr(re_mod, "compose", exploding)
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["sent"] == 1 and summary["failed"] == 1
        rows = {r.invoice_id: r for r in test_db_session.query(InvoiceReminderEmail).all()}
        assert rows[bad.id].status == "failed" and rows[bad.id].error_kind == "other"

    def test_lock_and_expected_mode(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "L/1", 100.0, TOMORROW)
        with pytest.raises(ModeMismatch):
            run_reminders(test_db_session, due_date=TOMORROW, mailer=FakeMailer(), cfg=_real_cfg(),
                          expected_mode="prova")
        assert test_db_session.query(InvoiceReminderEmail).count() == 0
        assert re_mod._reminder_lock.acquire(blocking=False)
        try:
            with pytest.raises(ReminderBusy):
                run_reminders(test_db_session, due_date=TOMORROW, mailer=FakeMailer(), cfg=_real_cfg())
        finally:
            re_mod._reminder_lock.release()

    def test_internal_mailer_is_closed(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "CL/1", 100.0, TOMORROW)
        created = []

        class Spy(FakeMailer):
            def __init__(self, api_key, api_url=None):
                super().__init__()
                created.append(self)

        monkeypatch.setattr("backend.connectors.resend_mail.ResendMailer", Spy)
        run_reminders(test_db_session, due_date=TOMORROW, cfg=_real_cfg())
        assert len(created) == 1 and created[0].closed is True and created[0].calls == 1

    def test_preview_writes_nothing_and_flags_what_will_run(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "PV/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "PV/0", 50.0, date.today() - timedelta(days=3), days_overdue=3)
        items = preview(test_db_session, TOMORROW, mode=MODE_PROVA)
        assert len(items) == 1 and items[0]["skip_reason"] is None and items[0]["will_process"] is True
        assert items[0]["overdue_count"] == 1 and items[0]["overdue_total"] == 50.0
        assert "scade domani" in items[0]["text"]
        assert test_db_session.query(InvoiceReminderEmail).count() == 0
        run_reminders(test_db_session, due_date=TOMORROW, mailer=FakeMailer(), cfg=_cfg())
        after = preview(test_db_session, TOMORROW, mode=MODE_PROVA)[0]
        assert after["already"] == "prepared" and after["will_process"] is False
        real = preview(test_db_session, TOMORROW, mode=MODE_REALE)[0]
        assert real["will_process"] is True and real["why"] == "invio dopo la prova"


# ── Connettore Resend ─────────────────────────────────────────────────

class TestResendMailer:
    def test_payload_attachments_and_idempotency(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["idem"] = request.headers.get("idempotency-key")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "abc-123"})

        mailer = ResendMailer("re_key", "https://api.resend.test", transport=httpx.MockTransport(handler))
        mid = mailer.send(
            sender="Sake Company <fatture@sakecompany.com>", to=["a@b.it"], subject="S",
            text="corpo", html="<p>corpo</p>", reply_to="fatture@sakecompany.com",
            bcc="luigi@sakecompany.com, dario@sakecompany.com",
            attachments=[("fattura.pdf", b"%PDF-1.4 test")], idempotency_key="reminder-1-2026-11-10-reale",
        )
        assert mid == "abc-123"
        assert seen["url"] == "https://api.resend.test/emails"
        assert seen["auth"] == "Bearer re_key" and seen["idem"] == "reminder-1-2026-11-10-reale"
        body = seen["body"]
        assert body["from"] == "Sake Company <fatture@sakecompany.com>" and body["to"] == ["a@b.it"]
        assert body["reply_to"] == ["fatture@sakecompany.com"]
        assert body["bcc"] == ["luigi@sakecompany.com", "dario@sakecompany.com"]
        assert body["attachments"] == [{"filename": "fattura.pdf", "content": base64.b64encode(b"%PDF-1.4 test").decode()}]
        assert body["html"] == "<p>corpo</p>" and body["text"] == "corpo"

    def test_error_kinds_and_no_key_in_message(self):
        def h422(request):
            return httpx.Response(422, json={"message": "Invalid `to` field", "name": "validation_error"})

        def h500(request):
            return httpx.Response(503, text="upstream down")

        def hnet(request):
            raise httpx.ConnectError("boom")

        for handler, kind, needle in ((h422, "validation", "Invalid"), (h500, "provider", "503"), (hnet, "network", "non raggiungibile")):
            mailer = ResendMailer("re_SEGRETISSIMA", "https://api.resend.test", transport=httpx.MockTransport(handler))
            with pytest.raises(MailSendError) as exc:
                mailer.send(sender="x@sakecompany.com", to=["a@b.it"], subject="S", text="t")
            assert exc.value.kind == kind and needle in str(exc.value)
            assert "SEGRETISSIMA" not in str(exc.value)

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
        assert data["target_due_date"] == TOMORROW.isoformat() and data["schedule"].startswith("09:15")
        assert data["preview"][0]["invoice_number"] == "API/1" and data["preview"][0]["will_process"] is True
        assert data["recent"] == []

        wrong = test_client.post("/api/reminders/email/run", json={"expected_mode": "reale"})
        assert wrong.status_code == 409 and "ricarica" in wrong.json()["detail"]
        run = test_client.post("/api/reminders/email/run", json={"expected_mode": "prova"})
        assert run.status_code == 200 and run.json()["prepared"] == 1

        log = test_client.get("/api/reminders/email/log", params={"customer_id": c.id})
        item = log.json()["items"][0]
        assert item["status"] == "prepared" and item["invoice_number"] == "API/1" and item["needs_manual_retry"] is False
        assert item["attachments"] == ["copia_cortesia_fattura_API_1.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]

        detail = test_client.get(f"/api/reminders/email/{item['id']}")
        assert detail.status_code == 200 and "scade domani" in detail.json()["body_text"]

        pdf = test_client.get(f"/api/reminders/email/{item['id']}/attachments/1")
        assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")
        assert pdf.content[:4] == b"%PDF"
        assert test_client.get(f"/api/reminders/email/{item['id']}/attachments/5").status_code == 404
        assert test_client.get("/api/reminders/email/999999").status_code == 404
        assert test_client.post(f"/api/reminders/email/{item['id']}/retry").status_code == 409  # non è ferma

        status2 = test_client.get("/api/reminders/email").json()
        assert status2["recent"][0]["invoice_id"] == inv.id and status2["preview"][0]["already"] == "prepared"
        assert status2["preview"][0]["will_process"] is False

    def test_busy_lock_returns_409(self, test_client):
        assert re_mod._reminder_lock.acquire(blocking=False)
        try:
            assert test_client.post("/api/reminders/email/run").status_code == 409
        finally:
            re_mod._reminder_lock.release()


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

    def test_job_never_raises_and_reports_busy(self, monkeypatch):
        from backend import scheduler as sched
        monkeypatch.setattr("backend.database.get_session_direct", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("backend.engine.reminder_email.run_reminders", lambda session: (_ for _ in ()).throw(RuntimeError("db giù")))
        assert "error" in sched.run_reminder_email_job()
        monkeypatch.setattr("backend.engine.reminder_email.run_reminders", lambda session: (_ for _ in ()).throw(ReminderBusy()))
        assert sched.run_reminder_email_job() == {"skipped": "busy"}

    def test_catchup_respects_window(self, monkeypatch):
        from backend import scheduler as sched
        monkeypatch.setattr("backend.engine.reminder_email.in_catchup_window", lambda: False)
        assert sched.run_reminder_email_catchup() == {"skipped": "window"}
        monkeypatch.setattr("backend.engine.reminder_email.in_catchup_window", lambda: True)
        monkeypatch.setattr(sched, "run_reminder_email_job", lambda: {"ran": True})
        assert sched.run_reminder_email_catchup() == {"ran": True}
