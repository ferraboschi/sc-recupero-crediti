"""Promemoria email il giorno prima della scadenza (backend/engine/reminder_email.py,
backend/engine/format.py, backend/connectors/resend_mail.py, backend/api/reminders.py,
job schedulato, PDF di backend/api/recovery.py).

Regole owner 9/11/2026: una email per le fatture in scadenza domani, con la
copia di cortesia di ciascuna e — se ci sono scadute non saldate — il PDF di
riepilogo e il totale nel testo. Dal 26/11/2026 una email per CLIENTE (prima:
una per fattura), con le scadenze a un giorno di distanza unite. Modalità prova di default (compone,
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
    agent_emails, carbon_copies, compose, due_label, in_catchup_window, needs_processing, normalize_email,
    overdue_invoices, preview, resolve_mode, retry_row, run_reminders, send_test, skip_reason,
    target_due_date, target_due_dates,
)

# Orologio FISSO per tutto il modulo: la logica usa l'ora italiana reale
# (italian_today / target_due_dates); senza fissarla la suite fallirebbe nel
# weekend e nelle ore in cui la data della macchina differisce da quella di
# Roma. Martedì 10/11/2026 alle 11:00 italiane → scadenza del giro: 11/11.
FIXED_NOW = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 10)
TOMORROW = date(2026, 11, 11)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr("backend.engine.format.datetime", _FixedDatetime)
    monkeypatch.setattr("backend.engine.reminder_email.datetime", _FixedDatetime)


def _cfg(**over):
    base = dict(
        REMINDER_EMAIL_ENABLED=False, RESEND_API_KEY="", RESEND_API_URL="https://api.resend.test",
        REMINDER_EMAIL_FROM="Sake Company <fatture@sakecompany.com>",
        REMINDER_EMAIL_REPLY_TO="fatture@sakecompany.com", REMINDER_EMAIL_BCC="",
        REMINDER_EMAIL_TEST_TO="", REMINDER_EMAIL_HOUR=9, REMINDER_EMAIL_MINUTE=15,
        REMINDER_EMAIL_CC="fatture@sakecompany.com",
        REMINDER_AGENT_EMAILS="Maggioni:g.maggioni@sakecompany.com;Izzo:a.izzo@sakecompany.com;Scotti:g.scotti@sakecompany.com",
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
        assert msg["subject"].startswith("Promemoria: fattura n. 2026/00001861/SAK in scadenza il")
        assert "540,00 €" in msg["subject"]
        assert "Spett.le Shabu Casatenovo" in msg["text"]
        assert "scade domani," in msg["text"] and "Copia della fattura in allegato" in msg["text"]
        assert "scadut" not in msg["text"]  # niente riga sulle scadute
        assert "IBAN" in msg["text"] and "causale" in msg["text"]
        assert "ignori questo messaggio" in msg["text"]
        assert msg["text"].count("\n\n") <= 5  # segnalazione corta: al massimo 6 blocchi
        assert msg["overdue_count"] == 0 and msg["overdue_total"] == 0
        assert "<p>" in msg["html"] and "Shabu Casatenovo" in msg["html"]

    def test_text_with_overdue_total_and_plural(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        o1 = _inv(test_db_session, c, "A/0", 1000.0, date.today() - timedelta(days=40), days_overdue=40)
        o2 = _inv(test_db_session, c, "A/00", 234.5, date.today() - timedelta(days=10), days_overdue=10)
        msg = compose(c, inv, [o1, o2])
        assert "Risultano inoltre scadute e non saldate 2 fatture per un totale di 1.234,50 €." in msg["text"]
        assert "Totale da saldare, compresa la fattura in scadenza: 1.334,50 €. Riepilogo in allegato." in msg["text"]
        assert msg["grand_total"] == 1334.5
        assert msg["overdue_count"] == 2 and msg["overdue_total"] == 1234.5
        one = compose(c, inv, [o1])
        assert "Risulta inoltre scaduta e non saldata 1 fattura per 1.000,00 €. Totale da saldare, compresa la fattura in scadenza: 1.100,00 €." in one["text"]

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
        # Allegato del promemoria: fattura in scadenza in cima («in scadenza»), scaduta coi giorni, totale di entrambe
        from backend.engine.reminder_email import build_attachments
        names, contents = zip(*build_attachments(c, inv, [o1]))
        assert names == ("copia_cortesia_fattura_C_1.pdf", "riepilogo_da_saldare_LAngolo_del_Sake.pdf")
        allegato = _pdf_strings(bytes(contents[1]))
        assert "Riepilogo Fatture da Saldare" in allegato
        assert "C/1" in allegato and "in scadenza" in allegato  # la fattura che genera l'email
        assert "C/0" in allegato and "40" in allegato            # la scaduta coi giorni di ritardo
        assert "2.234,50 EUR" in allegato                        # totale di entrambe (1.234,50 + 1.000,00)


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
        assert row.to_email == "amministrazione@shabu.it" and row.subject.startswith("Promemoria: fattura n.")
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
        assert names == ["copia_cortesia_fattura_R_1.pdf", "riepilogo_da_saldare_Shabu_Casatenovo.pdf"]
        assert all(content[:4] == b"%PDF" for _, content in sent["attachments"])
        assert "1 fattura per 1.000,00 €. Totale da saldare, compresa la fattura in scadenza: 1.100,00 €." in sent["text"]
        assert sent["cc"] == ["fatture@sakecompany.com"]  # cliente senza agente: solo la casella fatture
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
        assert mailer.sent[0]["cc"] is None  # in test nessuna copia a commerciale/fatture
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.to_email == "lorenzo@example.com" and row.original_to == "amministrazione@shabu.it"
        assert row.mode == MODE_TEST

    def test_email_is_normalized_before_sending(self, test_db_session):
        c = _cust(test_db_session, email="  Amministrazione@Shabu.IT ")
        _inv(test_db_session, c, "N/1", 100.0, TOMORROW)
        mailer = FakeMailer()
        run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert mailer.sent[0]["to"] == ["amministrazione@shabu.it"]

    def test_one_email_per_customer(self, test_db_session):
        """Decisione owner 26/11/2026: cinque fatture dello stesso cliente in
        scadenza = UNA email (prima: cinque)."""
        c = _cust(test_db_session)
        for n in range(5):
            _inv(test_db_session, c, f"M/{n}", 100.0 + n, TOMORROW)
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["sent"] == 1 and summary["invoices"] == 5 and mailer.calls == 1
        assert mailer.sent[0]["subject"] == f"Promemoria: 5 fatture in scadenza il 11/11/2026 ({fmt_eur(510.0)})"
        rows = test_db_session.query(InvoiceReminderEmail).all()
        assert len(rows) == 5 and {r.status for r in rows} == {"sent"}
        assert len({r.group_key for r in rows}) == 1 and rows[0].group_key

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
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        _inv(test_db_session, other, "GOOD/1", 100.0, TOMORROW)
        original = re_mod.compose

        def exploding(customer, invoice, overdue, today=None):
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


# ── Weekend, etichette, copie, invio di prova ─────────────────────────

class _Scripted:
    """Resend finto: `errors` = gli errori da sollevare, uno per chiamata; poi invia. Ricorda le chiavi."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.sent = []
        self.keys = []
        self.closed = False

    def send(self, **kwargs):
        self.keys.append(kwargs.get("idempotency_key"))
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(kwargs)
        return f"msg-{len(self.sent)}"

    def close(self):
        self.closed = True


def _timeout():
    return MailSendError("Resend non raggiungibile: ReadTimeout", kind=ERROR_NETWORK)


def _already_received():
    """Così il connettore riporta un 409 di Resend: un 4xx, quindi «validation»."""
    return MailSendError("Resend HTTP 409: Same idempotency key used with a different request payload",
                         kind=ERROR_VALIDATION)


def _refused():
    return MailSendError("Resend HTTP 422: invalid recipient", kind=ERROR_VALIDATION)


KEY = "reminder-{}-2026-11-11-reale"


class TestADoubtStaysADoubt:
    """Correzione 26/11/2026 (via libera owner): un promemoria dall'esito
    incerto resta incerto finché l'email non parte davvero. Prima, dopo un
    timeout, un «Riprova» a cui Resend rispondeva «già ricevuta» (409) lasciava
    scritto «rifiutata»: il registro diceva il falso, il giro la ritentava da
    solo e oltre le 24 ore un altro «Riprova» poteva mandare un doppione."""

    def _in_doubt(self, s, **cust):
        c = _cust(s, **cust)
        inv = _inv(s, c, "DUB/1", 100.0, TOMORROW)
        first = _Scripted(_timeout())
        run_reminders(s, due_date=TOMORROW, mailer=first, cfg=_real_cfg())
        row = s.query(InvoiceReminderEmail).one()
        assert (row.status, row.error_kind) == ("failed", "network") and first.keys == [KEY.format(inv.id)]
        # Nata un minuto fa secondo l'orologio dei test (il default del
        # database userebbe l'ora vera, e l'età verrebbe negativa).
        row.created_at = FIXED_NOW.replace(tzinfo=None) - timedelta(minutes=1)
        s.commit()
        return c, inv, row

    def test_already_received_is_recorded_as_such(self, test_db_session):
        _c, _i, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "conflict")
        assert "aveva già ricevuto un tentativo precedente" in row.error and len(row.error) <= 500
        assert re_mod.is_uncertain(row) and needs_processing(row, MODE_REALE)[0] is False

    def test_a_long_answer_from_resend_never_cuts_the_warning(self, test_db_session):
        _c, _i, row = self._in_doubt(test_db_session)
        long_409 = MailSendError("Resend HTTP 409: " + "x" * 900, kind=ERROR_VALIDATION)
        retry_row(test_db_session, row, mailer=_Scripted(long_409), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert len(row.error) <= 500 and row.error.endswith("controllare su Resend se è stata consegnata.")

    def test_in_prova_or_test_mode_the_panel_never_asks_for_the_confirmation(self, test_client, test_db_session, monkeypatch):
        """Fuori dal reale «Riprova» non manda nulla al cliente: niente «Invia comunque»."""
        from backend.config import config as real_config
        _c, _i, row = self._in_doubt(test_db_session)
        row.error_kind = "conflict"
        test_db_session.commit()
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", False)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "")
        item = test_client.get("/api/reminders/email/log").json()["items"][0]
        assert item["needs_manual_retry"] is True and item["retry_needs_confirmation"] is False
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", True)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "re_x")
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_TEST_TO", "lorenzo@example.com")
        item = test_client.get("/api/reminders/email/log").json()["items"][0]
        assert item["retry_needs_confirmation"] is False
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_TEST_TO", "")
        assert test_client.get("/api/reminders/email/log").json()["items"][0]["retry_needs_confirmation"] is True
        # Nel giorno della scadenza «Riprova» parte ancora.
        row.due_date = TODAY
        test_db_session.commit()
        assert test_client.get("/api/reminders/email/log").json()["items"][0]["retry_needs_confirmation"] is True
        # A scadenza passata «Riprova» non parte comunque: niente «Invia comunque…».
        row.due_date = TODAY - timedelta(days=1)
        test_db_session.commit()
        assert test_client.get("/api/reminders/email/log").json()["items"][0]["retry_needs_confirmation"] is False

    def test_already_received_is_never_retried_by_the_round(self, test_db_session):
        _c, _i, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=_real_cfg())
        again = _Scripted()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=again, cfg=_real_cfg())
        assert again.keys == [] and summary["sent"] == 0
        test_db_session.refresh(row)
        assert row.error_kind == "conflict"

    def test_after_already_received_riprova_needs_the_operator_confirmation(self, test_db_session):
        _c, inv, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert re_mod.needs_confirmation(row) is True
        plain = _Scripted()
        with pytest.raises(ValueError, match="controllato su Resend"):
            retry_row(test_db_session, row, mailer=plain, cfg=_real_cfg())
        assert plain.keys == []
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "conflict")   # resta com'è
        forced = _Scripted()
        assert retry_row(test_db_session, row, mailer=forced, cfg=_real_cfg(), force=True)["sent"] == 1
        # Tentativo NUOVO: chiave nuova, altrimenti Resend risponderebbe ancora 409.
        assert forced.keys[0] != KEY.format(inv.id) and forced.keys[0].startswith(KEY.format(inv.id) + "-f")
        test_db_session.refresh(row)
        assert row.status == "sent"

    def test_a_doubt_survives_a_refused_retry(self, test_db_session):
        """Timeout (forse arrivata), poi «Riprova» rifiutato: se diventasse
        «rifiutata» il giro la rimanderebbe da solo, e il registro mentirebbe."""
        _c, _i, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_refused()), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "network") and "422" in row.error
        again = _Scripted()
        run_reminders(test_db_session, due_date=TOMORROW, mailer=again, cfg=_real_cfg())
        assert again.keys == []

    def test_a_doubt_survives_a_retry_that_fails_on_the_pdf(self, test_db_session, monkeypatch):
        _c, _i, row = self._in_doubt(test_db_session)

        def broken(*args, **kwargs):
            raise RuntimeError("font mancante")

        monkeypatch.setattr(re_mod, "build_attachments", broken)
        mailer = _Scripted()
        retry_row(test_db_session, row, mailer=mailer, cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "network") and row.error.startswith("PDF non generato")
        assert mailer.keys == [] and needs_processing(row, MODE_REALE)[0] is False

    def test_a_row_interrupted_mid_send_stays_a_doubt_too(self, test_db_session):
        _c, _i, row = self._in_doubt(test_db_session)
        row.status, row.error_kind = "sending", None
        test_db_session.commit()
        retry_row(test_db_session, row, mailer=_Scripted(_refused()), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "other") and re_mod.is_uncertain(row)

    def test_a_doubt_is_never_rewritten_as_skipped(self, test_db_session):
        """Se oggi non si può inviare, la riga resta com'è e l'operatore legge il perché."""
        c, inv, row = self._in_doubt(test_db_session)
        for change, why in (
            (lambda: setattr(c, "email", None), "Cliente senza email"),
            (lambda: (setattr(c, "email", "amministrazione@shabu.it"), setattr(inv, "missing_streak", 1)), "Forse già pagata"),
        ):
            change()
            test_db_session.commit()
            mailer = _Scripted()
            with pytest.raises(ValueError, match=f"non inviabile ora: {why}"):
                retry_row(test_db_session, row, mailer=mailer, cfg=_real_cfg())
            test_db_session.refresh(row)
            assert (row.status, row.error_kind, row.skip_reason) == ("failed", "network", None) and mailer.keys == []

    def test_within_a_day_riprova_continues_the_same_attempt(self, test_db_session):
        """Stessa chiave: se il primo tentativo era arrivato, Resend risponde 409 e non rimanda."""
        _c, inv, row = self._in_doubt(test_db_session)
        assert re_mod.needs_confirmation(row) is False
        mailer = _Scripted()
        assert retry_row(test_db_session, row, mailer=mailer, cfg=_real_cfg())["sent"] == 1
        assert mailer.keys == [KEY.format(inv.id)]

    def test_after_a_day_resend_has_forgotten_and_riprova_needs_the_confirmation(self, test_db_session):
        _c, inv, row = self._in_doubt(test_db_session)
        now = FIXED_NOW.replace(tzinfo=None)
        row.created_at = now - timedelta(hours=22, minutes=59)
        test_db_session.commit()
        assert re_mod.needs_confirmation(row, now) is False
        row.created_at = now - timedelta(hours=23, minutes=1)
        test_db_session.commit()
        assert re_mod.needs_confirmation(row, now) is True
        plain = _Scripted()
        with pytest.raises(ValueError, match="controllato su Resend"):
            retry_row(test_db_session, row, mailer=plain, cfg=_real_cfg(), now_utc=FIXED_NOW)
        assert plain.keys == []
        # Confermato solo per l'età: si riusa la chiave di sempre. Se Resend
        # la ricorda ancora ferma lui il doppione (409), e lo si viene a sapere.
        remembered = _Scripted(_already_received())
        retry_row(test_db_session, row, mailer=remembered, cfg=_real_cfg(), now_utc=FIXED_NOW, force=True)
        test_db_session.refresh(row)
        assert remembered.keys == [KEY.format(inv.id)] and (row.status, row.error_kind) == ("failed", "conflict")

    def test_after_a_day_a_confirmed_retry_sends_with_the_usual_key(self, test_db_session):
        _c, inv, row = self._in_doubt(test_db_session)
        row.created_at = FIXED_NOW.replace(tzinfo=None) - timedelta(hours=30)
        test_db_session.commit()
        forced = _Scripted()
        assert retry_row(test_db_session, row, mailer=forced, cfg=_real_cfg(), now_utc=FIXED_NOW, force=True)["sent"] == 1
        test_db_session.refresh(row)
        assert forced.keys == [KEY.format(inv.id)]
        assert (row.status, row.error_kind, row.error) == ("sent", None, None)

    def test_a_real_doubt_is_retried_only_in_real_mode(self, test_db_session):
        """Altrimenti la riga cambierebbe modalità e perderebbe il suo dubbio:
        il prossimo «Riprova» in reale partirebbe senza nessuna cautela, e
        dopo 24 ore Resend non fermerebbe più il doppione."""
        _c, _i, row = self._in_doubt(test_db_session)
        for cfg in (_cfg(), _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")):
            mailer = _Scripted()
            with pytest.raises(ValueError, match="solo in modalità reale"):
                retry_row(test_db_session, row, mailer=mailer, cfg=cfg)
            test_db_session.refresh(row)
            assert (row.mode, row.status, row.error_kind) == ("reale", "failed", "network") and mailer.keys == []
        row.status, row.error_kind = "sending", None
        test_db_session.commit()
        with pytest.raises(ValueError, match="solo in modalità reale"):
            retry_row(test_db_session, row, mailer=_Scripted(), cfg=_cfg())

    def test_a_real_certain_failure_can_still_be_retried_in_any_mode(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "CERTA/3", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_refused()), cfg=_real_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        retry_row(test_db_session, row, mailer=_Scripted(), cfg=_cfg())
        test_db_session.refresh(row)
        assert (row.mode, row.status) == ("prova", "prepared")

    def test_in_test_mode_everything_is_as_before(self, test_db_session):
        """Il dubbio conta solo in reale: in test un 409 resta un rifiuto, e il
        giro reale manda poi il promemoria al cliente come ha sempre fatto."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "TEST/3", 100.0, TOMORROW)
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_timeout()), cfg=test_cfg)
        row = test_db_session.query(InvoiceReminderEmail).one()
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=test_cfg)
        test_db_session.refresh(row)
        assert (row.mode, row.status, row.error_kind) == ("test", "failed", "validation")
        assert "controllare su Resend" not in row.error
        mailer = _Scripted()
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())["sent"] == 1
        assert mailer.keys == [KEY.format(inv.id)] and mailer.sent[0]["to"] == ["amministrazione@shabu.it"]

    def test_in_test_mode_a_refused_retry_is_not_sticky(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "TEST/4", 100.0, TOMORROW)
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_timeout()), cfg=test_cfg)
        row = test_db_session.query(InvoiceReminderEmail).one()
        retry_row(test_db_session, row, mailer=_Scripted(_refused()), cfg=test_cfg)
        test_db_session.refresh(row)
        assert (row.mode, row.error_kind) == ("test", "validation")

    def test_already_received_stays_written_whatever_happens_next(self, test_db_session, monkeypatch):
        """Dopo un 409 ogni altro fallimento — timeout, rifiuto, PDF, errore
        imprevisto — lascia «conflict»: il pulsante resta «Invia comunque…»."""
        _c, _i, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=_real_cfg())
        for error in (_timeout(), _refused(), MailSendError("Resend HTTP 503", kind="provider"), RuntimeError("boom")):
            retry_row(test_db_session, row, mailer=_Scripted(error), cfg=_real_cfg(), force=True)
            test_db_session.refresh(row)
            assert (row.status, row.error_kind) == ("failed", "conflict"), error
            assert re_mod.needs_confirmation(row) is True

        def broken(*args, **kwargs):
            raise RuntimeError("font mancante")

        monkeypatch.setattr(re_mod, "build_attachments", broken)
        retry_row(test_db_session, row, mailer=_Scripted(), cfg=_real_cfg(), force=True)
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "conflict")

    def test_an_unexpected_error_during_a_retry_keeps_the_doubt(self, test_db_session, monkeypatch):
        _c, _i, row = self._in_doubt(test_db_session)

        def crash(*args, **kwargs):
            raise RuntimeError("database occupato")

        monkeypatch.setattr(re_mod, "compose", crash)
        retry_row(test_db_session, row, mailer=_Scripted(), cfg=_real_cfg())
        test_db_session.refresh(row)
        # Resta un dubbio (col tipo dell'ultimo errore), mai qualcosa che il giro ritenta da solo.
        assert (row.status, row.error_kind) == ("failed", "other") and row.error.startswith("Errore imprevisto")
        assert re_mod.is_uncertain(row) and needs_processing(row, MODE_REALE)[0] is False

    def test_already_received_survives_an_unexpected_error_too(self, test_db_session, monkeypatch):
        _c, _i, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=_real_cfg())

        def crash(*args, **kwargs):
            raise RuntimeError("database occupato")

        monkeypatch.setattr(re_mod, "compose", crash)
        retry_row(test_db_session, row, mailer=_Scripted(), cfg=_real_cfg(), force=True)
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "conflict") and row.error.startswith("Errore imprevisto")

    def test_every_confirmed_retry_after_a_409_has_its_own_key(self, test_db_session):
        _c, inv, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=_real_cfg())
        first, second = _Scripted(_timeout()), _Scripted()
        retry_row(test_db_session, row, mailer=first, cfg=_real_cfg(), force=True,
                  now_utc=FIXED_NOW + timedelta(seconds=1))
        retry_row(test_db_session, row, mailer=second, cfg=_real_cfg(), force=True,
                  now_utc=FIXED_NOW + timedelta(seconds=2))
        assert first.keys[0] != second.keys[0] and len(second.sent) == 1
        assert all(k.startswith(KEY.format(inv.id) + "-f") for k in first.keys + second.keys)

    def test_a_confirmed_retry_that_fails_again_is_still_a_doubt(self, test_db_session):
        _c, _i, row = self._in_doubt(test_db_session)
        retry_row(test_db_session, row, mailer=_Scripted(_already_received()), cfg=_real_cfg())
        retry_row(test_db_session, row, mailer=_Scripted(_refused()), cfg=_real_cfg(), force=True)
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "conflict") and re_mod.needs_confirmation(row) is True

    def test_force_changes_nothing_when_no_confirmation_is_needed(self, test_db_session):
        _c, inv, row = self._in_doubt(test_db_session)
        mailer = _Scripted()
        assert retry_row(test_db_session, row, mailer=mailer, cfg=_real_cfg(), force=True)["sent"] == 1
        assert mailer.keys == [KEY.format(inv.id)]

    def test_certain_failures_behave_as_before(self, test_db_session):
        """Rifiuto al PRIMO tentativo: certamente non partita. Il giro la ritenta da solo."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "CERTA/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_refused()), cfg=_real_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert (row.status, row.error_kind) == ("failed", "validation")
        assert not re_mod.is_uncertain(row) and not re_mod.needs_confirmation(row)
        row.created_at = FIXED_NOW.replace(tzinfo=None) - timedelta(days=3)   # nemmeno se vecchia
        test_db_session.commit()
        assert not re_mod.needs_confirmation(row, FIXED_NOW.replace(tzinfo=None))
        again = _Scripted()
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=again, cfg=_real_cfg())["sent"] == 1
        assert again.keys == [KEY.format(inv.id)]

    def test_a_409_on_the_first_attempt_is_a_doubt_too(self, test_db_session):
        """Chiave già vista da Resend senza che il registro lo sappia (riga persa, ripristino): comunque non si ritenta da soli."""
        c = _cust(test_db_session)
        _inv(test_db_session, c, "PRIMA/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_already_received()), cfg=_real_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert (row.status, row.error_kind) == ("failed", "conflict")

    def test_a_trial_doubt_is_a_new_send_in_real_mode(self, test_db_session):
        """L'incertezza riguarda la modalità in cui è nata: in test l'email andava all'indirizzo di prova."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "TEST/1", 100.0, TOMORROW)
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_timeout()), cfg=test_cfg)
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert (row.mode, row.status, row.error_kind) == ("test", "failed", "network")
        row.created_at = FIXED_NOW.replace(tzinfo=None) - timedelta(days=2)
        test_db_session.commit()
        assert re_mod.needs_confirmation(row, FIXED_NOW.replace(tzinfo=None)) is False
        retry_row(test_db_session, row, mailer=_Scripted(_refused()), cfg=_real_cfg(), now_utc=FIXED_NOW)
        test_db_session.refresh(row)
        # In reale è stato un rifiuto al primo tentativo: certamente non partita.
        assert (row.mode, row.status, row.error_kind) == ("reale", "failed", "validation")
        mailer = _Scripted()
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())["sent"] == 1
        assert mailer.keys == [KEY.format(inv.id)]

    def test_only_an_attempt_that_may_have_arrived_is_a_doubt(self, test_db_session):
        """Una riga saltata, preparata o inviata non è «in dubbio»: se poi il
        suo PRIMO invio reale viene rifiutato, è certamente non partita e il
        giro deve ritentarla da solo (altrimenti il promemoria andrebbe perso)."""
        for status in ("sent", "skipped", "prepared"):
            row = SimpleNamespace(status=status, error_kind=None, mode="reale", created_at=None, attempted_at=None)
            assert not re_mod.is_uncertain(row) and not re_mod.needs_confirmation(row)
        c = _cust(test_db_session, email=None)
        inv = _inv(test_db_session, c, "SALT/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(), cfg=_real_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert (row.status, row.skip_reason) == ("skipped", "senza_email")
        c.email = "amministrazione@shabu.it"
        test_db_session.commit()
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_refused()), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "validation")
        mailer = _Scripted()
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())["sent"] == 1
        assert mailer.keys == [KEY.format(inv.id)]

    def test_a_trial_then_a_refused_real_send_is_retried_by_the_round(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "PROVA/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(), cfg=_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.status == "prepared"
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_refused()), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.mode, row.status, row.error_kind) == ("reale", "failed", "validation")
        mailer = _Scripted()
        assert run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())["sent"] == 1
        assert mailer.keys == [KEY.format(inv.id)]

    def test_a_doubt_keeps_the_kind_of_its_latest_uncertain_failure(self, test_db_session):
        _c, _i, row = self._in_doubt(test_db_session)
        down = MailSendError("Resend HTTP 503: service unavailable", kind="provider")
        retry_row(test_db_session, row, mailer=_Scripted(down), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.status, row.error_kind) == ("failed", "provider") and "503" in row.error

    def test_a_certain_failure_can_still_become_skipped(self, test_db_session):
        """Solo l'incerto resta com'è: una riga certamente non partita, se oggi
        il cliente non ha più l'email, viene saltata come prima."""
        c = _cust(test_db_session)
        _inv(test_db_session, c, "CERTA/2", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_refused()), cfg=_real_cfg())
        row = test_db_session.query(InvoiceReminderEmail).one()
        c.email = None
        test_db_session.commit()
        retry_row(test_db_session, row, mailer=_Scripted(), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.status, row.skip_reason) == ("skipped", "senza_email")

    def test_a_trial_doubt_can_become_skipped_in_real_mode(self, test_db_session):
        """Il dubbio nato in test riguardava l'indirizzo di prova: in reale il cliente non ha ricevuto nulla."""
        c = _cust(test_db_session)
        _inv(test_db_session, c, "TEST/2", 100.0, TOMORROW)
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_timeout()), cfg=test_cfg)
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert (row.mode, row.error_kind) == ("test", "network")
        c.email = None
        test_db_session.commit()
        retry_row(test_db_session, row, mailer=_Scripted(), cfg=_real_cfg())
        test_db_session.refresh(row)
        assert (row.mode, row.status, row.skip_reason) == ("reale", "skipped", "senza_email")

    def test_the_age_of_the_doubt_is_measured_at_the_time_of_the_retry(self, test_db_session):
        _c, _i, row = self._in_doubt(test_db_session)
        row.created_at = FIXED_NOW.replace(tzinfo=None) - timedelta(hours=1)
        test_db_session.commit()
        later = FIXED_NOW + timedelta(hours=23)   # 24 ore dopo la nascita della riga
        plain = _Scripted()
        with pytest.raises(ValueError, match="controllato su Resend"):
            retry_row(test_db_session, row, mailer=plain, cfg=_real_cfg(), now_utc=later)
        assert plain.keys == []

    def test_needs_confirmation_without_dates_asks(self, test_db_session):
        row = SimpleNamespace(status="sending", error_kind=None, mode="reale", created_at=None, attempted_at=None)
        assert re_mod.needs_confirmation(row) is True
        row.mode = "prova"
        assert re_mod.needs_confirmation(row) is False

    def test_is_conflict(self):
        assert re_mod.is_conflict(_already_received()) and not re_mod.is_conflict(_refused())
        assert not re_mod.is_conflict(_timeout())
        odd = RuntimeError("altro")
        odd.status_code = 409
        assert re_mod.is_conflict(odd)

    def test_api_shows_invia_comunque_and_sends_only_when_confirmed(self, test_client, test_db_session, monkeypatch):
        from backend.config import config as real_config
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", True)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "re_x")
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_TEST_TO", "")
        created = []

        class Spy(_Scripted):
            def __init__(self, api_key, api_url=None):
                super().__init__()
                created.append(self)

        monkeypatch.setattr("backend.connectors.resend_mail.ResendMailer", Spy)
        _c, _i, row = self._in_doubt(test_db_session)
        item = test_client.get("/api/reminders/email/log").json()["items"][0]
        assert item["needs_manual_retry"] is True and item["retry_needs_confirmation"] is False
        row.error_kind = "conflict"
        test_db_session.commit()
        item = test_client.get("/api/reminders/email/log").json()["items"][0]
        assert item["needs_manual_retry"] is True and item["retry_needs_confirmation"] is True
        plain = test_client.post(f"/api/reminders/email/{row.id}/retry", json={"expected_mode": "reale"})
        assert plain.status_code == 409 and "controllato su Resend" in plain.json()["detail"]
        assert all(m.keys == [] for m in created)
        assert created and all(m.closed for m in created)   # anche su un rifiuto il client si chiude
        wrong = test_client.post(f"/api/reminders/email/{row.id}/retry", json={"expected_mode": "test", "force": True})
        assert wrong.status_code == 409 and all(m.closed for m in created) and all(m.keys == [] for m in created)
        forced = test_client.post(f"/api/reminders/email/{row.id}/retry", json={"expected_mode": "reale", "force": True})
        assert forced.status_code == 200 and forced.json()["sent"] == 1
        assert sum(len(m.sent) for m in created) == 1


class TestWeekendAndCopies:
    def test_target_due_dates_skip_weekend_and_anticipate_on_friday(self):
        # 2026-11-09 è lunedì (ora italiana: UTC+1 → 10:00Z = 11:00 CET)
        assert target_due_dates(datetime(2026, 11, 9, 10, tzinfo=timezone.utc)) == [date(2026, 11, 10)]
        assert target_due_dates(datetime(2026, 11, 12, 10, tzinfo=timezone.utc)) == [date(2026, 11, 13)]  # giovedì
        assert target_due_dates(datetime(2026, 11, 13, 10, tzinfo=timezone.utc)) == [
            date(2026, 11, 14), date(2026, 11, 15), date(2026, 11, 16)]  # venerdì → sab, dom, lun
        assert target_due_dates(datetime(2026, 11, 14, 10, tzinfo=timezone.utc)) == []  # sabato
        assert target_due_dates(datetime(2026, 11, 15, 10, tzinfo=timezone.utc)) == []  # domenica
        assert target_due_date(datetime(2026, 11, 14, 10, tzinfo=timezone.utc)) == date(2026, 11, 15)

    def test_invoice_label_strips_fatturapro_suffix(self):
        from backend.engine.reminder_email import invoice_label
        assert invoice_label("2026/00001804/SAK - Fattura") == "2026/00001804/SAK"
        assert invoice_label("2026/00001605/SAK - Nota di Credito") == "2026/00001605/SAK"
        assert invoice_label("234/2025") == "234/2025"
        assert invoice_label(None) == ""

    def test_compose_uses_clean_number_everywhere(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "2026/00001804/SAK - Fattura", 426.76, TOMORROW)
        msg = compose(c, inv, [])
        assert "fattura n. 2026/00001804/SAK di 426,76 €" in msg["text"]
        assert 'causale "Fattura 2026/00001804/SAK"' in msg["text"]
        assert " - Fattura" not in msg["subject"] and " - Fattura" not in msg["text"]
        from backend.engine.reminder_email import build_attachments
        assert build_attachments(c, inv, [])[0][0] == "copia_cortesia_fattura_2026_00001804_SAK.pdf"

    def test_due_label(self):
        today = date(2026, 11, 13)  # venerdì
        assert due_label(date(2026, 11, 14), today) == "domani, sabato 14/11/2026"
        assert due_label(date(2026, 11, 16), today) == "lunedì 16/11/2026"

    def test_agent_emails_parsing_and_default(self):
        assert agent_emails(_cfg()) == {
            "Maggioni": "g.maggioni@sakecompany.com", "Izzo": "a.izzo@sakecompany.com",
            "Scotti": "g.scotti@sakecompany.com"}
        assert agent_emails(_cfg(REMINDER_AGENT_EMAILS="izzo = A.Izzo@SakeCompany.com , Scotti:x")) == {
            "Izzo": "a.izzo@sakecompany.com"}
        from backend.config import config as real_config
        assert agent_emails(real_config)["Maggioni"] == "g.maggioni@sakecompany.com"

    def test_carbon_copies_agent_plus_fatture_dedup_and_exclude_to(self, test_db_session):
        c = _cust(test_db_session, tags="B2B, Izzo")
        assert carbon_copies(c, _cfg()) == ["a.izzo@sakecompany.com", "fatture@sakecompany.com"]
        c2 = _cust(test_db_session, nome="Due", email="fatture@sakecompany.com", partita_iva=None, tags="B2D, Maggioni, Scotti")
        assert carbon_copies(c2, _cfg()) == ["g.maggioni@sakecompany.com", "g.scotti@sakecompany.com"]
        c3 = _cust(test_db_session, nome="Tre", email="t@t.it", partita_iva=None, tags="B2B")
        assert carbon_copies(c3, _cfg(REMINDER_EMAIL_CC="")) == []

    def test_real_run_copies_agent_and_fatture(self, test_db_session):
        c = _cust(test_db_session, tags="B2B, rendina")
        _inv(test_db_session, c, "CC/1", 100.0, TOMORROW)
        mailer = FakeMailer()
        run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert mailer.sent[0]["to"] == ["amministrazione@shabu.it"]
        assert mailer.sent[0]["cc"] == ["a.izzo@sakecompany.com", "fatture@sakecompany.com"]
        row = test_db_session.query(InvoiceReminderEmail).one()
        assert row.cc_json == ["a.izzo@sakecompany.com", "fatture@sakecompany.com"]

    def test_friday_run_covers_three_due_dates(self, test_db_session):
        c = _cust(test_db_session)
        friday = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
        _inv(test_db_session, c, "SAB/1", 10.0, date(2026, 11, 14))
        _inv(test_db_session, c, "DOM/1", 20.0, date(2026, 11, 15))
        _inv(test_db_session, c, "LUN/1", 30.0, date(2026, 11, 16))
        _inv(test_db_session, c, "MAR/1", 40.0, date(2026, 11, 17))
        mailer = FakeMailer()
        summary = run_reminders(test_db_session, now_utc=friday, mailer=mailer, cfg=_real_cfg())
        assert summary["due_dates"] == ["2026-11-14", "2026-11-15", "2026-11-16"]
        # Una email per cliente: sabato, domenica e lunedì insieme, e con loro
        # anche martedì (scade il giorno dopo l'ultima scadenza del giro).
        assert summary["sent"] == 1 and len(mailer.sent) == 1
        assert summary["candidates"] == 3 and summary["invoices"] == 4 and summary["merged_early"] == 1
        text = mailer.sent[0]["text"]
        assert "- n. SAB/1 di 10,00 €, scade domani, sabato 14/11/2026" in text
        assert "- n. LUN/1 di 30,00 €, scade lunedì 16/11/2026" in text
        assert "- n. MAR/1 di 40,00 €, scade martedì 17/11/2026" in text
        saturday = datetime(2026, 11, 14, 10, tzinfo=timezone.utc)
        assert run_reminders(test_db_session, now_utc=saturday, mailer=mailer, cfg=_real_cfg())["candidates"] == 0
        # Lunedì il giro guarda martedì: già ricordata venerdì, non riparte.
        monday = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
        again = run_reminders(test_db_session, now_utc=monday, mailer=mailer, cfg=_real_cfg())
        assert again["candidates"] == 1 and again["already"] == 1 and again["sent"] == 0 and len(mailer.sent) == 1

    def test_real_mode_never_resends_test_rows_due_today(self, test_db_session):
        # Bug del 10/11/2026: lunedì in TEST per martedì; martedì si passa in
        # REALE e il giro delle 09:15 riprendeva ANCHE le righe test con
        # scadenza oggi → 3 email ai clienti il giorno stesso della scadenza.
        # Regola: promemoria il giorno prima, mai il giorno stesso. Parte solo
        # la scadenza di domani; la riga test di oggi resta com'è.
        c = _cust(test_db_session)
        monday = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)
        today_inv = _inv(test_db_session, c, "MAR/1", 30.0, TODAY)
        _inv(test_db_session, c, "MER/1", 40.0, TOMORROW)
        mailer = FakeMailer()
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        assert run_reminders(test_db_session, now_utc=monday, mailer=mailer, cfg=test_cfg)["sent"] == 1
        entries = preview(test_db_session, now_utc=FIXED_NOW, cfg=_real_cfg())
        assert [e["invoice_number"] for e in entries] == ["MER/1"]
        summary = run_reminders(test_db_session, now_utc=FIXED_NOW, mailer=mailer, cfg=_real_cfg())
        assert summary["sent"] == 1 and summary["candidates"] == 1 and "carried_over" not in summary
        assert [s["to"] for s in mailer.sent] == [["lorenzo@example.com"], ["amministrazione@shabu.it"]]
        assert "MER/1" in mailer.sent[1]["subject"]
        old = test_db_session.query(InvoiceReminderEmail).filter_by(invoice_id=today_inv.id).one()
        assert old.mode == "test" and old.status == "sent" and old.to_email == "lorenzo@example.com"
        later = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
        assert run_reminders(test_db_session, now_utc=later, mailer=mailer, cfg=_real_cfg())["sent"] == 0

    def test_friday_test_then_real_sends_nothing_on_weekend_nor_monday_rows(self, test_db_session):
        # Venerdì in TEST (copre sab/dom/lun), poi passaggio in REALE: sabato e
        # domenica non parte nulla (weekend), lunedì il giro guarda martedì e
        # NON riprende la riga test di lunedì (scade oggi). Il recupero giusto
        # è il giro manuale di venerdì stesso dopo il passaggio in reale.
        c = _cust(test_db_session)
        friday = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
        saturday = datetime(2026, 11, 14, 8, tzinfo=timezone.utc)
        monday = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
        lun = _inv(test_db_session, c, "LUN/1", 30.0, date(2026, 11, 16))
        _inv(test_db_session, c, "MAR/1", 40.0, date(2026, 11, 17))
        mailer = FakeMailer()
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        first = run_reminders(test_db_session, now_utc=friday, mailer=mailer, cfg=test_cfg)
        # Una email di prova con lunedì e, unita, martedì.
        assert first["sent"] == 1 and first["invoices"] == 2 and first["merged_early"] == 1
        sat = run_reminders(test_db_session, now_utc=saturday, mailer=mailer, cfg=_real_cfg())
        assert sat["due_dates"] == [] and sat["candidates"] == 0 and len(mailer.sent) == 1
        mon = run_reminders(test_db_session, now_utc=monday, mailer=mailer, cfg=_real_cfg())
        assert mon["due_dates"] == ["2026-11-17"] and mon["sent"] == 1
        assert [s["to"] for s in mailer.sent] == [["lorenzo@example.com"], ["amministrazione@shabu.it"]]
        assert "MAR/1" in mailer.sent[1]["subject"]
        assert test_db_session.query(InvoiceReminderEmail).filter_by(invoice_id=lun.id).one().mode == "test"
        # Venerdì stesso, dopo il passaggio in reale, il giro manuale copre
        # sab/dom/lun e riprende la riga test di lunedì per davvero.
        mailer2 = FakeMailer()
        _inv(test_db_session, c, "LUN/2", 50.0, date(2026, 11, 16))
        fri = run_reminders(test_db_session, now_utc=friday, mailer=mailer2, cfg=_real_cfg())
        # Una email reale con le due di lunedì; martedì è già partita davvero.
        assert fri["sent"] == 1 and fri["invoices"] == 2 and fri["merged_early"] == 0 and len(mailer2.sent) == 1
        assert "n. LUN/1 di" in mailer2.sent[0]["text"] and "n. LUN/2 di" in mailer2.sent[0]["text"]
        assert "MAR/1" not in mailer2.sent[0]["text"]
        assert mailer2.sent[0]["to"] == ["amministrazione@shabu.it"]

    def test_retry_refuses_past_due_dates(self, test_db_session):
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "OLD/1", 100.0, TODAY - timedelta(days=1))
        row = InvoiceReminderEmail(invoice_id=inv.id, customer_id=c.id, due_date=TODAY - timedelta(days=1),
                                   status="failed", mode="reale", error_kind="network")
        test_db_session.add(row)
        test_db_session.commit()
        with pytest.raises(ValueError, match="passata"):
            retry_row(test_db_session, row, mailer=FakeMailer(), cfg=_real_cfg())

    def test_preview_three_dates_with_labels(self, test_db_session):
        c = _cust(test_db_session)
        friday = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
        _inv(test_db_session, c, "SAB/1", 10.0, date(2026, 11, 14))
        _inv(test_db_session, c, "LUN/1", 30.0, date(2026, 11, 16))
        items = preview(test_db_session, now_utc=friday, mode=MODE_TEST, cfg=_cfg())
        assert [(i["invoice_number"], i["due_label"]) for i in items] == [
            ("SAB/1", "domani, sabato 14/11/2026"), ("LUN/1", "lunedì 16/11/2026")]
        assert items[0]["cc"] == [] and items[0]["would_cc"] == ["fatture@sakecompany.com"]

    def test_send_test_goes_only_to_chosen_address(self, test_db_session):
        c = _cust(test_db_session, tags="B2B, Maggioni")
        inv = _inv(test_db_session, c, "TST/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "TST/0", 50.0, date.today() - timedelta(days=3), days_overdue=3)
        mailer = FakeMailer()
        out = send_test(test_db_session, inv.id, " Lorenzo@EF-TI.com ", mailer=mailer, cfg=_real_cfg())
        sent = mailer.sent[0]
        assert sent["to"] == ["lorenzo@ef-ti.com"] and "cc" not in sent and "bcc" not in sent
        assert sent["subject"].startswith("[PROVA] Promemoria: fattura n. TST/1")
        assert [n for n, _ in sent["attachments"]] == ["copia_cortesia_fattura_TST_1.pdf", "riepilogo_da_saldare_Shabu_Casatenovo.pdf"]
        assert out["would_cc"] == ["g.maggioni@sakecompany.com", "fatture@sakecompany.com"]
        assert test_db_session.query(InvoiceReminderEmail).count() == 0  # registro intoccato
        with pytest.raises(ValueError):
            send_test(test_db_session, inv.id, "non-valida", mailer=mailer, cfg=_real_cfg())
        with pytest.raises(ValueError):
            send_test(test_db_session, inv.id, "a@b.it", mailer=mailer, cfg=_cfg())  # Resend non configurato
        # Mai a un cliente, nemmeno un altro cliente dell'anagrafica
        other = _cust(test_db_session, nome="Altro", email="altro@cliente.it", partita_iva=None)
        for addr in ("amministrazione@shabu.it", "Altro@Cliente.it"):
            with pytest.raises(ValueError, match="cliente"):
                send_test(test_db_session, inv.id, addr, mailer=mailer, cfg=_real_cfg())
        # Fattura inesistente / pagata / abbinamento dubbio
        from backend.engine.reminder_email import NotFound
        with pytest.raises(NotFound):
            send_test(test_db_session, 999999, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        inv.status = "paid"
        test_db_session.commit()
        with pytest.raises(ValueError, match="pagata"):
            send_test(test_db_session, inv.id, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        dubbia = _inv(test_db_session, other, "DUB/1", 10.0, TOMORROW, customer_name_raw="Altra Ditta Srl", customer_piva_raw="98765432100")
        with pytest.raises(ValueError, match="verificare"):
            send_test(test_db_session, dubbia.id, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        assert mailer.calls == 1  # solo il primo invio è partito


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
            cc=["g.maggioni@sakecompany.com", "fatture@sakecompany.com"],
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
        assert body["cc"] == ["g.maggioni@sakecompany.com", "fatture@sakecompany.com"]
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
        assert data["schedule"].startswith("09:15") and data["agent_emails"]["Izzo"] == "a.izzo@sakecompany.com"
        assert data["cc"] == "fatture@sakecompany.com"
        assert data["preview"][0]["invoice_number"] == "API/1" and data["preview"][0]["will_process"] is True
        assert data["recent"] == []

        wrong = test_client.post("/api/reminders/email/run", json={"expected_mode": "reale"})
        assert wrong.status_code == 409 and "ricarica" in wrong.json()["detail"]
        run = test_client.post("/api/reminders/email/run", json={"expected_mode": "prova"})
        assert run.status_code == 200 and run.json()["prepared"] == 1

        log = test_client.get("/api/reminders/email/log", params={"customer_id": c.id})
        item = log.json()["items"][0]
        assert item["status"] == "prepared" and item["invoice_number"] == "API/1" and item["needs_manual_retry"] is False
        assert item["attachments"] == ["copia_cortesia_fattura_API_1.pdf", "riepilogo_da_saldare_Shabu_Casatenovo.pdf"]

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

    def test_test_send_endpoint(self, test_client, test_db_session, monkeypatch):
        from backend.config import config as real_config
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "API/T", 100.0, TOMORROW)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "")
        assert test_client.post("/api/reminders/email/test", json={"invoice_id": inv.id, "to": "lorenzo@ef-ti.com"}).status_code == 409
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "re_x")
        created = []

        class Spy(FakeMailer):
            def __init__(self, api_key, api_url=None):
                super().__init__()
                created.append(self)

        monkeypatch.setattr("backend.connectors.resend_mail.ResendMailer", Spy)
        r = test_client.post("/api/reminders/email/test", json={"invoice_id": inv.id, "to": "lorenzo@ef-ti.com"})
        assert r.status_code == 200 and r.json()["sent_to"] == "lorenzo@ef-ti.com"
        assert created[0].sent[0]["to"] == ["lorenzo@ef-ti.com"] and created[0].closed

    def test_status_in_weekend(self, test_client, monkeypatch):
        monkeypatch.setattr("backend.api.reminders.target_due_dates", lambda: [])
        data = test_client.get("/api/reminders/email").json()
        assert data["weekend"] is True and data["preview"] == [] and data["target_due_date"] is None
        assert data["cc_effective"] == ["fatture@sakecompany.com"]

    def test_test_send_endpoint_errors(self, test_client, test_db_session, monkeypatch):
        from backend.config import config as real_config
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "re_x")
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "API/E", 100.0, TOMORROW)
        assert test_client.post("/api/reminders/email/test", json={"invoice_id": 999999, "to": "lorenzo@ef-ti.com"}).status_code == 404
        assert test_client.post("/api/reminders/email/test", json={"invoice_id": inv.id, "to": "amministrazione@shabu.it"}).status_code == 409

        class Failing(FakeMailer):
            def __init__(self, api_key, api_url=None):
                super().__init__(fail_times=1, kind=ERROR_VALIDATION)

        monkeypatch.setattr("backend.connectors.resend_mail.ResendMailer", Failing)
        r = test_client.post("/api/reminders/email/test", json={"invoice_id": inv.id, "to": "lorenzo@ef-ti.com"})
        assert r.status_code == 502 and "Resend HTTP 422" in r.json()["detail"]

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
        monkeypatch.setattr(sched, "run_followup_email_job", lambda: {"ran": "followup"})
        assert sched.run_reminder_email_catchup() == {"reminder": {"ran": True}, "followup": {"ran": "followup"}}


# ── Una email per cliente (decisione owner 26/11/2026) ────────────────

DAY_AFTER = date(2026, 11, 12)   # il giorno dopo la scadenza del giro: si unisce
GROUP_KEY = re.compile(r"^reminder-c\d+-20261110-[0-9a-f]{16}-reale$")


def _rows(s):
    s.expire_all()
    return {r.invoice_id: r for r in s.query(InvoiceReminderEmail).all()}


def _real_round(s, mailer=None, **kw):
    mailer = mailer or FakeMailer()
    return run_reminders(s, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg(), **kw), mailer


class TestOneEmailPerCustomer:
    """«Non si devono ricevere due mail. In caso di scadenze prossime unire le
    mail e adeguare il testo. Scadenza prossima significa ± un giorno.»"""

    def test_one_invoice_is_the_email_of_always(self, test_db_session):
        """Con una fattura sola nulla cambia: testo, allegati, chiave, righe."""
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "2026/00001804/SAK - Fattura", 426.76, TOMORROW)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        inv2 = _inv(test_db_session, other, "2026/00001900/SAK - Fattura", 100.0, TOMORROW)
        mailer = _Scripted()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["sent"] == 2 and summary["invoices"] == 2 and summary["merged_early"] == 0
        assert sorted(mailer.keys) == sorted([KEY.format(inv.id), KEY.format(inv2.id)])
        first = next(m for m in mailer.sent if m["to"] == ["amministrazione@shabu.it"])
        expected = compose(c, inv, [], TODAY)
        assert (first["subject"], first["text"], first["html"]) == (expected["subject"], expected["text"], expected["html"])
        assert [name for name, _ in first["attachments"]] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        rows = _rows(test_db_session)
        assert rows[inv.id].group_key is None and rows[inv.id].idempotency_key == KEY.format(inv.id)

    def test_two_invoices_due_the_same_day_make_one_email(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "2026/00001804/SAK - Fattura", 426.76, TOMORROW)
        b = _inv(test_db_session, c, "2026/00001812/SAK - Fattura", 310.0, TOMORROW)
        mailer = _Scripted()
        summary = run_reminders(test_db_session, due_date=TOMORROW, mailer=mailer, cfg=_real_cfg())
        assert summary["candidates"] == 2 and summary["sent"] == 1 and summary["invoices"] == 2
        [sent] = mailer.sent
        assert sent["subject"] == "Promemoria: 2 fatture in scadenza il 11/11/2026 (736,76 €)"
        assert sent["to"] == ["amministrazione@shabu.it"] and "fatture@sakecompany.com" in sent["cc"]
        text = sent["text"]
        assert "promemoria di cortesia: sono in scadenza le seguenti fatture:\n" in text
        assert "- n. 2026/00001804/SAK di 426,76 €, scade domani, mercoledì 11/11/2026" in text
        assert "- n. 2026/00001812/SAK di 310,00 €, scade domani, mercoledì 11/11/2026" in text
        assert "Totale: 736,76 €. Copie delle fatture in allegato." in text
        assert 'causale "Fatture 2026/00001804/SAK e 2026/00001812/SAK".' in text
        assert "Se ha già provveduto al pagamento, ignori questo messaggio." in text
        assert "<br>- n. 2026/00001812/SAK" in sent["html"]
        assert [name for name, _ in sent["attachments"]] == [
            "copia_cortesia_fattura_2026_00001804_SAK.pdf", "copia_cortesia_fattura_2026_00001812_SAK.pdf"]
        assert GROUP_KEY.match(mailer.keys[0])
        rows = _rows(test_db_session)
        assert {rows[a.id].status, rows[b.id].status} == {"sent"}
        assert rows[a.id].group_key == rows[b.id].group_key and rows[a.id].group_key in mailer.keys[0]
        assert rows[a.id].idempotency_key == rows[b.id].idempotency_key == mailer.keys[0]
        assert rows[a.id].provider_message_id == rows[b.id].provider_message_id == "msg-1"
        assert rows[a.id].subject == rows[b.id].subject == sent["subject"]
        assert rows[a.id].amount_due == 426.76 and rows[b.id].amount_due == 310.0
        assert rows[a.id].attachments_json == rows[b.id].attachments_json and len(rows[a.id].attachments_json) == 2
        # Un secondo giro non rimanda nulla.
        again, mailer2 = _real_round(test_db_session)
        assert again["already"] == 2 and mailer2.calls == 0

    def test_an_invoice_due_the_day_after_joins_todays_email_and_gets_no_email_of_its_own(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        summary, mailer = _real_round(test_db_session)
        assert summary["candidates"] == 1 and summary["sent"] == 1 and summary["invoices"] == 2
        assert summary["merged_early"] == 1
        [sent] = mailer.sent
        assert sent["subject"] == "Promemoria: 2 fatture in scadenza (150,00 €)"
        assert "- n. A/1 di 100,00 €, scade domani, mercoledì 11/11/2026" in sent["text"]
        assert "- n. B/1 di 50,00 €, scade giovedì 12/11/2026" in sent["text"]
        rows = _rows(test_db_session)
        assert rows[b.id].status == "sent" and rows[b.id].due_date == DAY_AFTER
        assert rows[a.id].group_key == rows[b.id].group_key
        # Il giorno dopo il giro guarda il 12/11: già ricordata, nessuna seconda email.
        tomorrow = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
        mailer2 = FakeMailer()
        nxt = run_reminders(test_db_session, now_utc=tomorrow, mailer=mailer2, cfg=_real_cfg())
        assert nxt["due_dates"] == ["2026-11-12"] and nxt["already"] == 1 and mailer2.calls == 0

    def test_an_invoice_due_the_day_after_does_nothing_on_its_own(self, test_db_session):
        """Da sola aspetta il suo giorno: né email, né righe, né conteggi."""
        c = _cust(test_db_session)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        summary, mailer = _real_round(test_db_session)
        assert summary["candidates"] == 0 and mailer.calls == 0 and _rows(test_db_session) == {}
        assert preview(test_db_session, due_date=TOMORROW, mode=MODE_REALE, cfg=_real_cfg()) == []
        # Il suo giorno parte da sola, con l'email di sempre.
        mailer2 = _Scripted()
        nxt = run_reminders(test_db_session, due_date=DAY_AFTER, mailer=mailer2, cfg=_real_cfg())
        assert nxt["sent"] == 1 and mailer2.keys == [f"reminder-{b.id}-2026-11-12-reale"]
        assert mailer2.sent[0]["subject"].startswith("Promemoria: fattura n. B/1 in scadenza il 12/11/2026")

    def test_an_excluded_invoice_of_the_day_after_writes_nothing_today(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER, due_date_source="assumed")
        summary, mailer = _real_round(test_db_session)
        assert summary["sent"] == 1 and summary["invoices"] == 1 and summary["skipped"] == 0
        assert "B/1" not in mailer.sent[0]["text"] and mailer.sent[0]["subject"].startswith("Promemoria: fattura n. A/1")
        rows = _rows(test_db_session)
        assert set(rows) == {a.id} and rows[a.id].group_key is None
        # Il suo giorno il giro scrive perché non parte.
        nxt = run_reminders(test_db_session, due_date=DAY_AFTER, mailer=FakeMailer(), cfg=_real_cfg())
        assert nxt["skipped"] == 1 and _rows(test_db_session)[b.id].skip_reason == "scadenza_stimata"

    def test_without_a_ready_invoice_of_the_round_nothing_leaves(self, test_db_session):
        """La fattura del giro è esclusa: quella del giorno dopo non parte al
        suo posto oggi; parte il suo giorno, da sola."""
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW, missing_streak=1)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        summary, mailer = _real_round(test_db_session)
        assert mailer.calls == 0 and summary["skipped"] == 1 and summary["sent"] == 0
        rows = _rows(test_db_session)
        assert set(rows) == {a.id} and rows[a.id].skip_reason == "forse_pagata"
        mailer2 = FakeMailer()
        nxt = run_reminders(test_db_session, due_date=DAY_AFTER, mailer=mailer2, cfg=_real_cfg())
        assert nxt["sent"] == 1 and "B/1" in mailer2.sent[0]["subject"] and _rows(test_db_session)[b.id].group_key is None

    def test_overdue_invoices_are_quoted_once_with_the_grand_total(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        _inv(test_db_session, c, "OLD/1", 200.0, date(2026, 10, 1), days_overdue=40)
        _inv(test_db_session, c, "OLD/2", 25.5, date(2026, 10, 20), days_overdue=21)
        summary, mailer = _real_round(test_db_session)
        [sent] = mailer.sent
        assert "Totale di queste fatture: 150,00 €. Copie delle fatture in allegato." in sent["text"]
        assert ("Risultano inoltre scadute e non saldate 2 fatture per un totale di 225,50 €. "
                "Totale da saldare, comprese le fatture in scadenza: 375,50 €. Riepilogo in allegato.") in sent["text"]
        names = [name for name, _ in sent["attachments"]]
        assert names == ["copia_cortesia_fattura_A_1.pdf", "copia_cortesia_fattura_B_1.pdf",
                         "riepilogo_da_saldare_Shabu_Casatenovo.pdf"]
        riepilogo = _pdf_strings(sent["attachments"][2][1])
        for numero in ("A/1", "B/1", "OLD/1", "OLD/2"):
            assert numero in riepilogo
        row = next(iter(_rows(test_db_session).values()))
        assert row.overdue_count == 2 and row.overdue_total == 225.5

    def test_one_overdue_invoice_is_said_in_the_singular(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        _inv(test_db_session, c, "OLD/1", 200.0, date(2026, 10, 1), days_overdue=40)
        _summary, mailer = _real_round(test_db_session)
        assert "Risulta inoltre scaduta e non saldata 1 fattura per 200,00 €." in mailer.sent[0]["text"]

    def test_beyond_three_invoices_the_reason_for_payment_does_not_list_them(self, test_db_session):
        c = _cust(test_db_session)
        for n in range(4):
            _inv(test_db_session, c, f"N/{n}", 10.0, TOMORROW)
        _summary, mailer = _real_round(test_db_session)
        assert "indicando nella causale i numeri delle fatture." in mailer.sent[0]["text"]
        assert 'causale "' not in mailer.sent[0]["text"]

    def test_three_invoices_are_listed_in_the_reason_for_payment(self, test_db_session):
        c = _cust(test_db_session)
        for n in range(3):
            _inv(test_db_session, c, f"N/{n}", 10.0, TOMORROW)
        _summary, mailer = _real_round(test_db_session)
        assert 'causale "Fatture N/0, N/1 e N/2".' in mailer.sent[0]["text"]

    def test_two_documents_with_the_same_number_get_distinct_file_names(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "2026/00001804/SAK - Fattura", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "2026/00001804/SAK - Nota di debito", 50.0, TOMORROW)
        _summary, mailer = _real_round(test_db_session)
        names = [name for name, _ in mailer.sent[0]["attachments"]]
        assert sorted(names) == sorted([f"copia_cortesia_fattura_2026_00001804_SAK_{a.id}.pdf",
                                        f"copia_cortesia_fattura_2026_00001804_SAK_{b.id}.pdf"])
        assert 'causale "Fattura 2026/00001804/SAK".' in mailer.sent[0]["text"]

    def test_each_customer_gets_their_own_email(self, test_db_session):
        c = _cust(test_db_session)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "A/2", 100.0, DAY_AFTER)
        _inv(test_db_session, other, "X/1", 70.0, TOMORROW)
        _inv(test_db_session, other, "X/2", 30.0, TOMORROW)
        summary, mailer = _real_round(test_db_session)
        assert summary["sent"] == 2 and summary["invoices"] == 4
        by_to = {m["to"][0]: m["text"] for m in mailer.sent}
        assert "A/1" in by_to["amministrazione@shabu.it"] and "X/1" not in by_to["amministrazione@shabu.it"]
        assert "X/2" in by_to["info@altro.it"] and "A/2" not in by_to["info@altro.it"]
        keys = {r.group_key for r in _rows(test_db_session).values()}
        assert len(keys) == 2 and None not in keys

    def test_an_invoice_of_another_customer_can_never_enter_the_email(self, test_db_session):
        c = _cust(test_db_session)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        x = _inv(test_db_session, other, "X/1", 70.0, TOMORROW)
        plan = re_mod.plan_customer(test_db_session, [a, x], [TOMORROW], MODE_REALE, customer_id=c.id)
        assert [i.id for i in plan.email] == [a.id]
        summary = {"sent": 0, "failed": 0, "prepared": 0, "already": 0, "items": []}
        with pytest.raises(ValueError, match="altro cliente"):
            re_mod._process_group(test_db_session, c, [a, x], key="k", mode=MODE_REALE, mailer=FakeMailer(),
                                  cfg=_real_cfg(), summary=summary, existing={}, today=TODAY,
                                  idempotency_key="k")
        assert _rows(test_db_session) == {}

    def test_an_invoice_paid_while_the_round_runs_leaves_the_email(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        invoices = [a, b]
        b.status = "paid"
        test_db_session.commit()
        plan = re_mod.plan_customer(test_db_session, invoices, [TOMORROW], MODE_REALE)
        assert [i.id for i in plan.email] == [a.id] and plan.candidates == 1

    def test_prova_prepares_one_email_and_reale_then_sends_it(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        first = run_reminders(test_db_session, due_date=TOMORROW, cfg=_cfg())
        assert first["prepared"] == 1 and first["invoices"] == 2 and first["sent"] == 0
        rows = _rows(test_db_session)
        assert {rows[a.id].status, rows[b.id].status} == {"prepared"} and rows[a.id].group_key == rows[b.id].group_key
        summary, mailer = _real_round(test_db_session)
        assert summary["sent"] == 1 and summary["invoices"] == 2 and mailer.calls == 1
        rows = _rows(test_db_session)
        assert {rows[a.id].status, rows[b.id].status} == {"sent"} and {rows[a.id].mode, rows[b.id].mode} == {"reale"}

    def test_a_certain_refusal_fails_the_whole_email_and_the_round_retries_it_as_one(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        failing = _Scripted(_refused())
        first = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert first["failed"] == 1 and first["sent"] == 0
        rows = _rows(test_db_session)
        assert {(r.status, r.error_kind) for r in rows.values()} == {("failed", "validation")}
        second = run_reminders(test_db_session, due_date=TOMORROW, mailer=failing, cfg=_real_cfg())
        assert second["sent"] == 1 and second["invoices"] == 2 and len(failing.sent) == 1
        assert failing.keys[0] == failing.keys[1]
        rows = _rows(test_db_session)
        assert {r.status for r in rows.values()} == {"sent"} and rows[a.id].error is None and rows[b.id].error_kind is None

    def test_a_pdf_failure_fails_the_whole_email_without_sending(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        monkeypatch.setattr(re_mod, "build_group_attachments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fpdf giù")))
        summary, mailer = _real_round(test_db_session)
        assert summary["failed"] == 1 and mailer.calls == 0
        assert {(r.status, r.error_kind) for r in _rows(test_db_session).values()} == {("failed", "pdf")}

    def test_an_unexpected_error_marks_every_invoice_of_the_email_and_spares_the_others(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        x = _inv(test_db_session, other, "X/1", 70.0, TOMORROW)
        original = re_mod.compose_many

        def exploding(customer, invoices, overdue, today=None):
            if len(invoices) > 1:
                raise RuntimeError("boom")
            return original(customer, invoices, overdue, today)

        monkeypatch.setattr(re_mod, "compose_many", exploding)
        summary, mailer = _real_round(test_db_session)
        assert summary["sent"] == 1 and summary["failed"] == 1 and mailer.calls == 1
        rows = _rows(test_db_session)
        assert (rows[a.id].status, rows[a.id].error_kind) == ("failed", "other") == (rows[b.id].status, rows[b.id].error_kind)
        assert rows[x.id].status == "sent"

    def test_a_customer_that_breaks_the_plan_does_not_stop_the_others(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        x = _inv(test_db_session, other, "X/1", 70.0, TOMORROW)
        original = re_mod.plan_customer

        def broken(session, invoices, targets, mode, **kw):
            if kw.get("customer_id") == c.id:
                raise RuntimeError("piano rotto")
            return original(session, invoices, targets, mode, **kw)

        monkeypatch.setattr(re_mod, "plan_customer", broken)
        summary, mailer = _real_round(test_db_session)
        assert summary["broken"] == 1 and summary["sent"] == 1 and set(_rows(test_db_session)) == {x.id}

    def test_if_another_round_took_one_invoice_the_email_does_not_leave(self, test_db_session, monkeypatch):
        from sqlalchemy.exc import IntegrityError
        c = _cust(test_db_session)
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        real_commit = test_db_session.commit
        state = {"n": 0}

        def commit():
            state["n"] += 1
            if state["n"] == 1:
                raise IntegrityError("insert", {}, Exception("uq_reminder_invoice_due"))
            return real_commit()

        monkeypatch.setattr(test_db_session, "commit", commit)
        summary, mailer = _real_round(test_db_session)
        assert summary["already"] == 2 and summary["sent"] == 0 and mailer.calls == 0

    def test_the_second_notice_still_finds_the_reminder_of_each_invoice(self, test_db_session):
        from backend.engine import followup_email as fu
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        _real_round(test_db_session)
        assert fu.reminder_sent(test_db_session, a).invoice_id == a.id
        assert fu.reminder_sent(test_db_session, b).due_date == DAY_AFTER


class TestOneEmailPerCustomerDoubts:
    """Un esito incerto resta incerto, anche per un'email con più fatture:
    «Riprova» rimanda l'email INTERA, con la chiave del tentativo incerto."""

    def _in_doubt(self, s):
        c = _cust(s)
        a = _inv(s, c, "A/1", 100.0, TOMORROW)
        b = _inv(s, c, "B/1", 50.0, DAY_AFTER)
        first = _Scripted(_timeout())
        run_reminders(s, due_date=TOMORROW, mailer=first, cfg=_real_cfg())
        rows = _rows(s)
        assert {(r.status, r.error_kind) for r in rows.values()} == {("failed", "network")}
        assert rows[a.id].idempotency_key == rows[b.id].idempotency_key == first.keys[0]
        return c, a, b, first.keys[0]

    def test_the_round_never_resends_an_email_in_doubt(self, test_db_session):
        _c, _a, _b, _key = self._in_doubt(test_db_session)
        summary, mailer = _real_round(test_db_session)
        assert mailer.calls == 0 and summary["already"] == 1
        # Nemmeno il giorno dopo, quando la fattura unita è «del giro».
        nxt = run_reminders(test_db_session, due_date=DAY_AFTER, mailer=mailer, cfg=_real_cfg())
        assert mailer.calls == 0 and nxt["already"] == 1

    def test_riprova_on_one_row_resends_the_whole_email_with_the_same_key(self, test_db_session):
        _c, a, b, key = self._in_doubt(test_db_session)
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=mailer, cfg=_real_cfg())
        assert out["sent"] == 1 and out["invoices"] == 2 and out["candidates"] == 2
        assert mailer.keys == [key] and "A/1" in mailer.sent[0]["text"] and "B/1" in mailer.sent[0]["text"]
        assert {r.status for r in _rows(test_db_session).values()} == {"sent"}

    def test_already_received_marks_the_whole_email_and_asks_for_a_confirmation(self, test_db_session):
        _c, a, b, key = self._in_doubt(test_db_session)
        retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=_Scripted(_already_received()), cfg=_real_cfg())
        rows = _rows(test_db_session)
        assert {(r.status, r.error_kind) for r in rows.values()} == {("failed", "conflict")}
        plain = _Scripted()
        with pytest.raises(ValueError, match="controllato su Resend"):
            retry_row(test_db_session, rows[b.id], mailer=plain, cfg=_real_cfg())
        assert plain.keys == []
        forced = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=forced, cfg=_real_cfg(), force=True)
        assert out["sent"] == 1 and out["invoices"] == 2
        assert forced.keys[0] != key and re.search(r"-reale-f\d{20}$", forced.keys[0])
        assert {r.status for r in _rows(test_db_session).values()} == {"sent"}

    def test_a_refusal_after_a_doubt_does_not_make_the_email_certain(self, test_db_session):
        _c, a, b, key = self._in_doubt(test_db_session)
        retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=_Scripted(_refused()), cfg=_real_cfg())
        assert {(r.status, r.error_kind) for r in _rows(test_db_session).values()} == {("failed", "network")}
        summary, mailer = _real_round(test_db_session)
        assert mailer.calls == 0   # il giro non la riprende da solo

    def test_an_invoice_paid_meanwhile_stays_out_and_the_key_is_still_the_one_in_doubt(self, test_db_session):
        """Il primo tentativo forse è arrivato. Se «Riprova» usasse una chiave
        nuova perché le fatture sono cambiate, Resend non fermerebbe il doppione."""
        _c, a, b, key = self._in_doubt(test_db_session)
        a.status = "paid"
        test_db_session.commit()
        remembered = _Scripted(_already_received())
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=remembered, cfg=_real_cfg())
        assert remembered.keys == [key] and out["failed"] == 1
        rows = _rows(test_db_session)
        assert (rows[b.id].status, rows[b.id].error_kind) == ("failed", "conflict")
        assert (rows[a.id].status, rows[a.id].error_kind) == ("failed", "network")   # non toccata

    def test_an_accepted_retry_frees_the_key_of_an_invoice_left_out(self, test_db_session):
        """Resend accetta il ritento con la chiave in dubbio: non l'aveva mai
        vista, quindi nulla era arrivato. La fattura rimasta fuori (oggi non
        inviabile) non deve più usare quella chiave: il suo ritento, più
        tardi, prenderebbe un 409 per un'email che di lei non parla."""
        _c, a, b, key = self._in_doubt(test_db_session)
        a.missing_streak = 1
        test_db_session.commit()
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=mailer, cfg=_real_cfg())
        assert out["sent"] == 1 and out["invoices"] == 1 and mailer.keys == [key] and "A/1" not in mailer.sent[0]["text"]
        rows = _rows(test_db_session)
        assert rows[b.id].status == "sent"
        assert (rows[a.id].status, rows[a.id].error_kind, rows[a.id].idempotency_key) == ("failed", "network", None)
        a.missing_streak = 0
        test_db_session.commit()
        later = _Scripted()
        assert retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=later, cfg=_real_cfg())["sent"] == 1
        assert later.keys == [KEY.format(a.id)] and later.sent[0]["subject"].startswith("Promemoria: fattura n. A/1")

    def test_riprova_on_a_row_not_sendable_today_is_refused_and_touches_nothing(self, test_db_session):
        _c, a, b, key = self._in_doubt(test_db_session)
        a.missing_streak = 1
        test_db_session.commit()
        mailer = _Scripted()
        with pytest.raises(ValueError, match="non inviabile ora"):
            retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=mailer, cfg=_real_cfg())
        assert mailer.keys == []
        assert {(r.status, r.error_kind, r.idempotency_key) for r in _rows(test_db_session).values()} == {
            ("failed", "network", key)}

    def test_a_real_doubt_is_not_retried_in_test_mode(self, test_db_session):
        _c, a, b, key = self._in_doubt(test_db_session)
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        mailer = _Scripted()
        with pytest.raises(ValueError, match="solo in modalità reale"):
            retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=mailer, cfg=test_cfg)
        assert mailer.keys == [] and {r.mode for r in _rows(test_db_session).values()} == {"reale"}

    def test_after_a_day_the_whole_email_needs_the_confirmation_and_keeps_its_key(self, test_db_session):
        _c, a, b, key = self._in_doubt(test_db_session)
        for row in _rows(test_db_session).values():   # nate all'ora del giro (orologio del test)
            row.created_at = FIXED_NOW.replace(tzinfo=None)
        test_db_session.commit()
        # Poco prima delle 23 ore «Riprova» basta: Resend ricorda ancora la chiave.
        soon = FIXED_NOW + timedelta(hours=22, minutes=30)
        assert not re_mod.needs_confirmation(_rows(test_db_session)[b.id], soon.replace(tzinfo=None))
        # La fattura unita scade il 12/11: l'11/11 alle 11:30 la sua riga ha più di 23 ore.
        later = datetime(2026, 11, 11, 10, 30, tzinfo=timezone.utc)
        b_row = _rows(test_db_session)[b.id]
        plain = _Scripted()
        with pytest.raises(ValueError, match="controllato su Resend"):
            retry_row(test_db_session, b_row, mailer=plain, cfg=_real_cfg(), now_utc=later)
        assert plain.keys == []
        forced = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=forced, cfg=_real_cfg(), force=True,
                        now_utc=later)
        # Confermata solo per l'età: la chiave resta quella (se Resend la
        # ricorda ancora, ferma lui il doppione).
        assert forced.keys == [key] and out["sent"] == 1

    def test_a_retry_is_refused_when_the_due_date_of_the_invoice_has_changed(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_timeout()), cfg=_real_cfg())
        a.due_date = date(2026, 11, 20)
        test_db_session.commit()
        mailer = _Scripted()
        with pytest.raises(ValueError, match="scadenza della fattura è cambiata"):
            retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=mailer, cfg=_real_cfg())
        assert mailer.keys == []

    def test_a_certain_failure_retried_by_hand_with_a_reason_today_says_why(self, test_db_session):
        """Come nel giro: ciò che certamente non era partito e oggi non è
        inviabile diventa «saltata» col motivo (nessun dubbio da custodire)."""
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_refused()), cfg=_real_cfg())
        c.email = None
        test_db_session.commit()
        out = retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=_Scripted(), cfg=_real_cfg())
        assert out["skipped"] == 1 and _rows(test_db_session)[a.id].skip_reason == "senza_email"


class TestOneEmailPerCustomerPreviewAndTest:
    def test_the_preview_shows_the_one_email_with_its_invoices(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        x = _inv(test_db_session, other, "X/1", 70.0, TOMORROW)
        _inv(test_db_session, other, "X/2", 30.0, DAY_AFTER, due_date_source="assumed")   # non parte oggi
        items = {e["invoice_id"]: e for e in preview(test_db_session, due_date=TOMORROW, mode=MODE_REALE, cfg=_real_cfg())}
        assert set(items) == {a.id, b.id, x.id}
        assert items[a.id]["group_size"] == 2 and items[a.id]["group_invoices"] == ["B/1"] and not items[a.id]["merged_early"]
        assert items[b.id]["merged_early"] and items[b.id]["group_invoices"] == ["A/1"] and items[b.id]["will_process"]
        assert items[b.id]["due_date"] == "2026-11-12" and items[b.id]["due_label"] == "giovedì 12/11/2026"
        assert items[a.id]["subject"] == items[b.id]["subject"] == "Promemoria: 2 fatture in scadenza (150,00 €)"
        assert items[a.id]["text"] == items[b.id]["text"] and "- n. B/1 di 50,00 €" in items[a.id]["text"]
        assert items[x.id]["group_size"] == 1 and items[x.id]["subject"].startswith("Promemoria: fattura n. X/1")
        assert test_db_session.query(InvoiceReminderEmail).count() == 0   # nessuna scrittura

    def test_the_preview_is_what_the_round_then_sends(self, test_db_session):
        c = _cust(test_db_session)
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        _inv(test_db_session, c, "OLD/1", 200.0, date(2026, 10, 1), days_overdue=40)
        shown = preview(test_db_session, due_date=TOMORROW, mode=MODE_REALE, cfg=_real_cfg())
        _summary, mailer = _real_round(test_db_session)
        assert {(e["subject"], e["text"]) for e in shown} == {(mailer.sent[0]["subject"], mailer.sent[0]["text"])}

    def test_the_test_send_is_the_email_the_customer_would_get_today(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        mailer = FakeMailer()
        out = send_test(test_db_session, a.id, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == ["A/1", "B/1"] and out["subject"] == "[PROVA] Promemoria: 2 fatture in scadenza (150,00 €)"
        assert mailer.sent[0]["to"] == ["lorenzo@ef-ti.com"] and len(mailer.sent[0]["attachments"]) == 2
        assert "cc" not in mailer.sent[0] or not mailer.sent[0].get("cc")
        assert test_db_session.query(InvoiceReminderEmail).count() == 0

    def test_the_test_send_of_an_invoice_outside_todays_round_is_the_single_email(self, test_db_session):
        c = _cust(test_db_session)
        far = _inv(test_db_session, c, "FAR/1", 100.0, date(2026, 11, 20))
        _inv(test_db_session, c, "A/1", 50.0, TOMORROW)
        mailer = FakeMailer()
        out = send_test(test_db_session, far.id, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == ["FAR/1"] and out["subject"].startswith("[PROVA] Promemoria: fattura n. FAR/1")


class TestOneEmailPerCustomerBelts:
    """Le cinture: ognuna chiude una strada che i test sopra non percorrono."""

    def _in_doubt(self, s, third=False):
        c = _cust(s)
        a = _inv(s, c, "A/1", 100.0, TOMORROW)
        b = _inv(s, c, "B/1", 50.0, DAY_AFTER)
        first = _Scripted(_timeout())
        run_reminders(s, due_date=TOMORROW, mailer=first, cfg=_real_cfg())
        return c, a, b, first.keys[0]

    def _mixed(self, s):
        """Cintura: due righe della STESSA email con esiti diversi — A «già
        ricevuta» (409), B in dubbio semplice. Il motore non lo produce (le
        righe di un'email si chiudono insieme, e un ritento parziale le
        stacca dal gruppo): si scrive a mano."""
        c, a, b, key = self._in_doubt(s)
        rows = _rows(s)
        rows[a.id].error_kind = "conflict"
        s.commit()
        rows = _rows(s)
        assert rows[a.id].group_key == rows[b.id].group_key
        return c, a, b, key

    def test_rows_retried_on_their_own_leave_the_email_they_were_in(self, test_db_session):
        """Un ritento parziale stacca le righe dal gruppo: le altre non le
        trascinano più con sé."""
        _c, a, b, key = self._in_doubt(test_db_session)
        b.missing_streak = 1
        test_db_session.commit()
        retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=_Scripted(_already_received()), cfg=_real_cfg())
        b.missing_streak = 0
        test_db_session.commit()
        rows = _rows(test_db_session)
        assert (rows[a.id].error_kind, rows[a.id].group_key) == ("conflict", None) and rows[b.id].group_key
        mailer = _Scripted()
        out = retry_row(test_db_session, rows[b.id], mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == 1 and "A/1" not in mailer.sent[0]["text"] and mailer.keys == [key]
        assert _rows(test_db_session)[a.id].error_kind == "conflict"

    def test_a_received_mark_wins_over_a_simple_doubt(self):
        row = lambda kind: SimpleNamespace(status="failed", error_kind=kind, mode="reale")  # noqa: E731
        assert re_mod._carried_doubt_many([row("network"), row("conflict")], MODE_REALE) == "conflict"
        assert re_mod._carried_doubt_many([row("network"), None, row("provider")], MODE_REALE) == "network"
        assert re_mod._carried_doubt_many([row("pdf"), None], MODE_REALE) is None
        assert re_mod._carried_doubt_many([row("conflict")], MODE_TEST) is None

    def test_an_invoice_with_another_due_date_never_enters_the_plan(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        far = _inv(test_db_session, c, "FAR/1", 50.0, date(2026, 11, 20))
        past = _inv(test_db_session, c, "PAST/1", 50.0, TODAY)
        plan = re_mod.plan_customer(test_db_session, [a, far, past], [TOMORROW], MODE_REALE)
        assert [i.id for i in plan.email] == [a.id] and plan.candidates == 1 and plan.skipped == []

    def test_the_test_send_groups_the_invoices_even_without_the_customer_email(self, test_db_session):
        c = _cust(test_db_session, email=None)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        mailer = FakeMailer()
        out = send_test(test_db_session, a.id, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == ["A/1", "B/1"] and len(mailer.sent[0]["attachments"]) == 2

    def test_the_invoices_of_the_email_are_never_counted_among_the_overdue_ones(self, test_db_session):
        """Anche se il campo dei giorni di ritardo è rimasto indietro."""
        c = _cust(test_db_session)
        _inv(test_db_session, c, "A/1", 100.0, TOMORROW, days_overdue=3)
        _inv(test_db_session, c, "B/1", 50.0, TOMORROW, days_overdue=3)
        _summary, mailer = _real_round(test_db_session)
        assert "scadut" not in mailer.sent[0]["text"] and len(mailer.sent[0]["attachments"]) == 2
        assert "Totale: 150,00 €." in mailer.sent[0]["text"]

    def test_attachments_follow_the_order_of_the_list(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, DAY_AFTER)
        names = [name for name, _ in re_mod.build_group_attachments(c, [b, a], [])]
        assert names == ["copia_cortesia_fattura_A_1.pdf", "copia_cortesia_fattura_B_1.pdf"]

    def test_a_skipped_invoice_no_longer_belongs_to_the_failed_email(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_refused()), cfg=_real_cfg())
        assert all(r.group_key and r.idempotency_key for r in _rows(test_db_session).values())
        b.missing_streak = 1
        test_db_session.commit()
        summary, mailer = _real_round(test_db_session)
        rows = _rows(test_db_session)
        assert summary["sent"] == 1 and summary["skipped"] == 1 and "B/1" not in mailer.sent[0]["text"]
        assert (rows[b.id].status, rows[b.id].group_key, rows[b.id].idempotency_key) == ("skipped", None, None)
        assert (rows[a.id].status, rows[a.id].group_key) == ("sent", None)

    def test_an_error_after_the_email_has_left_never_turns_it_into_a_failure(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        original, state = re_mod._item, {"n": 0}

        def flaky(row, invoice, customer):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("riepilogo rotto")
            return original(row, invoice, customer)

        monkeypatch.setattr(re_mod, "_item", flaky)
        summary, mailer = _real_round(test_db_session)
        rows = _rows(test_db_session)
        assert mailer.calls == 1 and {rows[a.id].status, rows[b.id].status} == {"sent"}
        assert summary["sent"] == 1 and summary["failed"] == 0

    def test_a_received_mark_on_one_row_holds_for_the_whole_retried_email(self, test_db_session):
        """Ritento confermato scelto dalla riga in dubbio semplice, con dentro
        quella «già ricevuta»: se fallisce ancora, restano TUTTE «già ricevute»."""
        _c, a, b, key = self._mixed(test_db_session)
        mailer = _Scripted(_timeout())
        retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=mailer, cfg=_real_cfg(), force=True)
        assert mailer.keys[0] != key and "-f2026" in mailer.keys[0]
        assert {(r.status, r.error_kind) for r in _rows(test_db_session).values()} == {("failed", "conflict")}

    def test_without_the_confirmation_a_received_row_stays_out_of_the_retry(self, test_db_session):
        _c, a, b, key = self._mixed(test_db_session)
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == 1 and "A/1" not in mailer.sent[0]["text"] and mailer.keys == [key]
        rows = _rows(test_db_session)
        assert (rows[a.id].status, rows[a.id].error_kind) == ("failed", "conflict") and rows[b.id].status == "sent"

    def test_an_accepted_retry_keeps_the_key_on_the_rows_it_sent(self, test_db_session):
        _c, a, b, key = self._in_doubt(test_db_session)
        a.missing_streak = 1
        test_db_session.commit()
        retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=_Scripted(), cfg=_real_cfg())
        rows = _rows(test_db_session)
        assert rows[b.id].idempotency_key == key and rows[a.id].idempotency_key is None

    def test_after_a_day_an_accepted_retry_does_not_free_the_key_of_the_row_left_out(self, test_db_session):
        """Oltre le 24 ore Resend può aver dimenticato la chiave: che accetti il
        ritento non prova che il primo tentativo non fosse arrivato."""
        _c, a, b, key = self._in_doubt(test_db_session)
        for row in _rows(test_db_session).values():
            row.created_at = FIXED_NOW.replace(tzinfo=None)
        a.missing_streak = 1
        test_db_session.commit()
        later = FIXED_NOW + timedelta(hours=23, minutes=30)
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=mailer, cfg=_real_cfg(), force=True,
                        now_utc=later)
        assert out["sent"] == 1 and mailer.keys == [key]
        rows = _rows(test_db_session)
        assert (rows[a.id].status, rows[a.id].error_kind, rows[a.id].idempotency_key) == ("failed", "network", key)

    def test_a_row_already_sent_is_never_resent_by_the_retry_of_another(self, test_db_session):
        """Cintura: due righe con la stessa chiave d'email ma esiti diversi
        (non dovrebbe accadere) — quella inviata non riparte."""
        _c, a, b, key = self._in_doubt(test_db_session)
        rows = _rows(test_db_session)
        rows[a.id].status, rows[a.id].error_kind, rows[a.id].error = "sent", None, None
        test_db_session.commit()
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == 1 and "A/1" not in mailer.sent[0]["text"]

    def test_an_invoice_moved_to_another_customer_stays_out_of_the_retry(self, test_db_session):
        c, a, b, key = self._in_doubt(test_db_session)
        other = _cust(test_db_session, "Altro Srl", "info@altro.it", partita_iva="01234567897")
        b.customer_id = other.id
        b.audit_reviewed_at = datetime(2026, 11, 10, 9, 0)   # abbinamento confermato: nessun altro motivo la esclude
        test_db_session.commit()
        assert skip_reason(b, other) is None
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == 1 and "B/1" not in mailer.sent[0]["text"] and mailer.sent[0]["to"] == ["amministrazione@shabu.it"]
        rows = _rows(test_db_session)
        assert (rows[b.id].status, rows[b.id].error_kind, rows[b.id].customer_id) == ("failed", "network", c.id)

    def test_an_invoice_whose_due_date_changed_or_passed_stays_out_of_the_retry(self, test_db_session, monkeypatch):
        _c, a, b, key = self._in_doubt(test_db_session)
        b.due_date = date(2026, 11, 20)
        test_db_session.commit()
        mailer = _Scripted()
        assert retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=mailer, cfg=_real_cfg())["invoices"] == 1
        assert "B/1" not in mailer.sent[0]["text"] and _rows(test_db_session)[b.id].status == "failed"

    def test_the_retry_of_the_merged_invoice_leaves_out_the_one_whose_due_date_has_passed(self, test_db_session, monkeypatch):
        _c, a, b, key = self._in_doubt(test_db_session)
        monkeypatch.setattr(re_mod, "italian_today", lambda *args, **kw: DAY_AFTER)   # il 12/11: A scadeva l'11
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[b.id], mailer=mailer, cfg=_real_cfg(),
                        now_utc=FIXED_NOW + timedelta(hours=2))
        assert out["invoices"] == 1 and "A/1" not in mailer.sent[0]["text"] and mailer.keys == [key]
        assert _rows(test_db_session)[a.id].status == "failed"
        with pytest.raises(ValueError, match="scadenza già passata"):
            retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=_Scripted(), cfg=_real_cfg())

    def test_two_separate_emails_of_the_same_customer_are_never_merged_by_a_retry(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        far = _inv(test_db_session, c, "C/1", 70.0, date(2026, 11, 13))
        run_reminders(test_db_session, due_date=TOMORROW, mailer=_Scripted(_timeout()), cfg=_real_cfg())
        run_reminders(test_db_session, due_date=date(2026, 11, 13), mailer=_Scripted(_timeout()), cfg=_real_cfg())
        rows = _rows(test_db_session)
        assert rows[a.id].group_key is None and rows[far.id].group_key is None
        mailer = _Scripted()
        out = retry_row(test_db_session, rows[a.id], mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == 1 and mailer.keys == [KEY.format(a.id)] and "C/1" not in mailer.sent[0]["text"]
        assert _rows(test_db_session)[far.id].status == "failed"

    def test_a_doubt_born_in_test_mode_is_a_new_send_in_real_mode(self, test_db_session):
        """La chiave di un tentativo di PROVA non si riusa per l'invio vero:
        Resend la ricorderebbe (409) e il cliente non riceverebbe nulla."""
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        b = _inv(test_db_session, c, "B/1", 50.0, TOMORROW)
        test_cfg = _real_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        first = _Scripted(_timeout())
        run_reminders(test_db_session, due_date=TOMORROW, mailer=first, cfg=test_cfg)
        assert first.keys[0].endswith("-test")
        mailer = _Scripted()
        out = retry_row(test_db_session, _rows(test_db_session)[a.id], mailer=mailer, cfg=_real_cfg())
        assert out["sent"] == 1 and out["invoices"] == 2
        assert mailer.keys[0].endswith("-reale") and mailer.keys[0] != first.keys[0]
        assert mailer.sent[0]["to"] == ["amministrazione@shabu.it"]
        assert {(r.status, r.mode) for r in _rows(test_db_session).values()} == {("sent", "reale")}
        assert b.id in _rows(test_db_session)


class TestOneEmailPerCustomerAfterTheRound:
    """Dopo il giro: ciò che si mostra (anteprima, prova, allegati) è l'email
    partita, con le fatture che aveva — non una ricomposta oggi."""

    def _sent_together(self, s):
        c = _cust(s)
        a = _inv(s, c, "A/1", 100.0, TOMORROW)
        b = _inv(s, c, "B/1", 50.0, TOMORROW)
        early = _inv(s, c, "C/1", 30.0, DAY_AFTER)
        _summary, mailer = _real_round(s)
        assert len(mailer.sent) == 1
        return c, a, b, early, mailer.sent[0]

    def test_the_preview_after_the_round_shows_the_email_that_left(self, test_db_session):
        _c, a, b, early, sent = self._sent_together(test_db_session)
        items = {e["invoice_id"]: e for e in preview(test_db_session, due_date=TOMORROW, mode=MODE_REALE, cfg=_real_cfg())}
        assert set(items) == {a.id, b.id}
        for inv, others in ((a, ["B/1", "C/1"]), (b, ["A/1", "C/1"])):
            entry = items[inv.id]
            assert entry["already"] == "sent" and not entry["will_process"]
            assert (entry["subject"], entry["text"]) == (sent["subject"], sent["text"])
            assert entry["group_size"] == 3 and entry["group_invoices"] == others
        # Il giorno dopo la fattura unita è «del giro»: stessa email, non un testo suo.
        nxt = preview(test_db_session, due_date=DAY_AFTER, mode=MODE_REALE, cfg=_real_cfg())
        [entry] = nxt
        assert entry["invoice_id"] == early.id and entry["subject"] == sent["subject"] == "Promemoria: 3 fatture in scadenza (180,00 €)"
        assert entry["group_invoices"] == ["A/1", "B/1"]

    def test_the_preview_of_a_single_email_already_sent_is_its_own_text(self, test_db_session):
        c = _cust(test_db_session)
        a = _inv(test_db_session, c, "A/1", 100.0, TOMORROW)
        _summary, mailer = _real_round(test_db_session)
        [entry] = preview(test_db_session, due_date=TOMORROW, mode=MODE_REALE, cfg=_real_cfg())
        assert (entry["subject"], entry["text"]) == (mailer.sent[0]["subject"], mailer.sent[0]["text"])
        assert entry["group_size"] == 1 and entry["group_invoices"] == [] and a.id == entry["invoice_id"]

    def test_the_test_send_after_the_round_is_the_email_that_left(self, test_db_session):
        _c, a, b, early, sent = self._sent_together(test_db_session)
        mailer = FakeMailer()
        out = send_test(test_db_session, a.id, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        assert out["invoices"] == ["A/1", "B/1", "C/1"] and out["subject"] == f"[PROVA] {sent['subject']}"
        assert mailer.sent[0]["text"] == sent["text"] and len(mailer.sent[0]["attachments"]) == 3
        # Una nel frattempo saldata non c'è più: si prova con quelle ancora da pagare.
        b.status, b.amount_due = "paid", 0
        test_db_session.commit()
        again = send_test(test_db_session, early.id, "lorenzo@ef-ti.com", mailer=mailer, cfg=_real_cfg())
        assert again["invoices"] == ["A/1", "C/1"]

    def test_an_attachment_of_one_email_is_always_the_document_with_that_name(self, test_client, test_db_session, monkeypatch):
        from backend.config import config as real_config
        from backend.engine.merge import merge_customers
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", False)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "")
        c, a, b, early, _sent = self._sent_together(test_db_session)
        rows = _rows(test_db_session)
        names = rows[b.id].attachments_json
        assert names == ["copia_cortesia_fattura_A_1.pdf", "copia_cortesia_fattura_B_1.pdf", "copia_cortesia_fattura_C_1.pdf"]

        def fetch(invoice, index):
            return test_client.get(f"/api/reminders/email/{_rows(test_db_session)[invoice.id].id}/attachments/{index}")

        def number_in(response):
            return next(n for n in ("A/1", "B/1", "C/1") if n in _pdf_strings(response.content))

        # Da qualunque riga dell'email, il pulsante n dà il documento n.
        for invoice in (a, b, early):
            assert [number_in(fetch(invoice, i)) for i in range(3)] == ["A/1", "B/1", "C/1"]
            assert f'filename="{names[1]}"' in fetch(invoice, 1).headers["content-disposition"]
            assert fetch(invoice, 3).status_code == 404
        # Scheda unita a un'altra: le fatture cambiano cliente, i nomi no.
        survivor = _cust(test_db_session, "Shabu Casatenovo Srl", "amministrazione@shabu.it", partita_iva="01234567897")
        merge_customers(test_db_session, survivor, c)
        test_db_session.commit()
        assert [number_in(fetch(b, i)) for i in range(3)] == ["A/1", "B/1", "C/1"]
        # Una fattura sparita dal DB: il suo allegato non c'è più, e nessun altro documento ne prende il nome.
        test_db_session.query(Invoice).filter(Invoice.id == a.id).delete()
        test_db_session.commit()
        assert fetch(b, 0).status_code == 404
        assert number_in(fetch(b, 1)) == "B/1" and number_in(fetch(b, 2)) == "C/1"

    def test_the_registry_api_says_which_invoices_travelled_together(self, test_client, test_db_session, monkeypatch):
        from backend.config import config as real_config
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", False)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", "")
        c, a, b, early, _sent = self._sent_together(test_db_session)
        recent = {r["invoice_id"]: r for r in test_client.get("/api/reminders/email").json()["recent"]}
        assert recent[a.id]["group_size"] == 3
        assert [m["invoice_number"] for m in recent[a.id]["group_invoices"]] == ["B/1", "C/1"]
        log = {r["invoice_id"]: r for r in test_client.get("/api/reminders/email/log", params={"customer_id": c.id}).json()["items"]}
        assert [m["invoice_number"] for m in log[early.id]["group_invoices"]] == ["A/1", "B/1"]
        detail = test_client.get(f"/api/reminders/email/{recent[b.id]['id']}").json()
        assert detail["group_size"] == 3 and "sono in scadenza le seguenti fatture" in detail["body_text"]
