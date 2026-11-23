"""Secondo avviso email: fattura scaduta da N giorni e ancora non saldata
(backend/engine/followup_email.py, API /api/reminders/followup…, job del mattino).

Richiesta owner 24/11/2026: una settimana dopo la scadenza, se la fattura del
promemoria risulta ancora non pagata, parte «un'ulteriore email» più diretta:
abbiamo rilevato una fattura scaduta da 7 giorni, chiediamo il saldo.
Qui ogni regola ha il suo test: quando parte, a chi, una volta sola, mai su
un dato di pagamento non aggiornato, spento di default, promemoria intoccato.
"""
import re
import zlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.connectors.resend_mail import MailSendError
from backend.database import (
    Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail, RecoveryAction, RecoveryActionInvoice,
    RecoveryCase, SyncState,
)
from backend.engine import followup_email as fu
from backend.engine import reminder_email as re_mod
from backend.engine.followup_email import (
    FOLLOWUP, MODE_OFF, candidate_invoices, compose_followup, followup_days, followup_extra_skip,
    followup_lookback, followup_skip_reason, followup_window, max_sync_age_hours, other_overdue_invoices,
    preview_followups, reminder_sent, resolve_followup_mode, retry_followup_row, run_followups,
    send_followup_test, sync_gate,
)
from backend.engine.reminder_email import (
    ERROR_NETWORK, ERROR_VALIDATION, MODE_PROVA, MODE_REALE, MODE_TEST, ModeMismatch, NotFound, ReminderBusy,
    run_reminders,
)

# Orologio FISSO: mercoledì 18/11/2026 alle 11:00 italiane. Una fattura con
# scadenza mercoledì 11/11 è scaduta da 7 giorni esatti; la finestra del giro
# (7 giorni + 6 di recupero) va dal 05/11 all'11/11.
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


def _at(day: date, hour: int = 10) -> datetime:
    """Le `hour` UTC del giorno dato (10 UTC = 11:00 italiane a novembre)."""
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)


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


def _on_cfg(**over):
    """Secondo avviso ACCESO e invio reale configurato."""
    over.setdefault("REMINDER_FOLLOWUP_ENABLED", True)
    return _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_test_key_SEGRETA", **over)


def _cust(s, nome="Shabu Casatenovo", email="amministrazione@shabu.it", **kw):
    c = Customer(ragione_sociale=nome, email=email, partita_iva=kw.pop("partita_iva", "12345670785"),
                 excluded=kw.pop("excluded", False), recovery_status=kw.pop("recovery_status", "idle"),
                 source="shopify", **kw)
    s.add(c)
    s.commit()
    return c


def _inv(s, cust, num, amount, due, status="open", days_overdue=None, **kw):
    """Fattura ben abbinata con scadenza REALE; `days_overdue` di default è
    quello che il sync avrebbe scritto oggi."""
    if days_overdue is None:
        days_overdue = max((TODAY - due).days, 0)
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


def _reminded(s, inv, mode="reale", status="sent", due=None):
    """Il promemoria pre-scadenza della fattura, come lo lascia il giro."""
    due = due or inv.due_date
    row = InvoiceReminderEmail(
        invoice_id=inv.id, customer_id=inv.customer_id, due_date=due, status=status, mode=mode,
        to_email="amministrazione@shabu.it", subject="Promemoria", amount_due=inv.amount_due,
        sent_at=datetime(due.year, due.month, due.day, 8, 15) - timedelta(days=1) if status == "sent" else None,
    )
    s.add(row)
    s.commit()
    return row


def _due(s, cust, num="2026/00001804/SAK - Fattura", amount=426.76, due=DUE7, **kw):
    """Fattura scaduta nella finestra, col promemoria già inviato al cliente."""
    inv = _inv(s, cust, num, amount, due, **kw)
    _reminded(s, inv)
    return inv


def _sync(s, now=FIXED_NOW, minutes_ago=15, success=True, partial=False):
    """Stato dell'ultimo sync delle fatture (tabella sync_state)."""
    when = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    row = s.query(SyncState).filter_by(key="invoices").first() or SyncState(key="invoices")
    row.last_sync = when
    row.result = {"fatturapro": {"success": success, "partial": partial, "error": None if success else "login"}}
    s.add(row)
    s.commit()
    return row


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
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.S):
        chunk = m.group(1)
        try:
            out.append(zlib.decompress(chunk).decode("latin-1"))
        except Exception:
            out.append(chunk.decode("latin-1", "replace"))
    return "\n".join(out)


def _rows(s):
    return s.query(InvoiceFollowupEmail).order_by(InvoiceFollowupEmail.id).all()


# ── Configurazione e calendario ───────────────────────────────────────

class TestConfig:
    def test_off_by_default_even_with_real_reminders(self):
        assert resolve_followup_mode(_cfg()) == (
            MODE_OFF, ["REMINDER_FOLLOWUP_ENABLED", "REMINDER_EMAIL_ENABLED", "RESEND_API_KEY"])
        real_reminders = _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_x")
        assert resolve_followup_mode(real_reminders) == (MODE_OFF, ["REMINDER_FOLLOWUP_ENABLED"])

    def test_enabled_follows_reminder_mode(self):
        assert resolve_followup_mode(_on_cfg()) == (MODE_REALE, [])
        assert resolve_followup_mode(_on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")) == (MODE_TEST, [])
        mode, missing = resolve_followup_mode(_cfg(REMINDER_FOLLOWUP_ENABLED=True))
        assert mode == MODE_PROVA and "RESEND_API_KEY" in missing

    def test_settings_survive_bad_values(self):
        assert (followup_days(_cfg()), followup_lookback(_cfg()), max_sync_age_hours(_cfg())) == (7, 6, 3)
        bad = _cfg(REMINDER_FOLLOWUP_DAYS="sette", REMINDER_FOLLOWUP_LOOKBACK_DAYS="", REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS=None)
        assert (followup_days(bad), followup_lookback(bad), max_sync_age_hours(bad)) == (7, 6, 3)
        low = _cfg(REMINDER_FOLLOWUP_DAYS="0", REMINDER_FOLLOWUP_LOOKBACK_DAYS="0", REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS="-4")
        # Mai «0 giorni dopo la scadenza», mai meno di un weekend di recupero.
        assert (followup_days(low), followup_lookback(low), max_sync_age_hours(low)) == (1, 2, 1)
        assert followup_days(_cfg(REMINDER_FOLLOWUP_DAYS=" 10 ")) == 10
        # Un valore enorme non deve far saltare il calcolo delle date (né il pannello).
        huge = _cfg(REMINDER_FOLLOWUP_DAYS="100000000", REMINDER_FOLLOWUP_LOOKBACK_DAYS="999999999",
                    REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS="100000")
        assert (followup_days(huge), followup_lookback(huge), max_sync_age_hours(huge)) == (365, 60, 48)
        assert followup_window(FIXED_NOW, huge) == (TODAY - timedelta(days=425), TODAY - timedelta(days=365))
        missing = SimpleNamespace()
        assert (followup_days(missing), followup_lookback(missing), max_sync_age_hours(missing)) == (7, 6, 3)

    def test_window_weekdays_and_weekend(self):
        assert followup_window(FIXED_NOW, _cfg()) == (date(2026, 11, 5), date(2026, 11, 11))
        assert followup_window(_at(date(2026, 11, 21)), _cfg()) is None  # sabato
        assert followup_window(_at(date(2026, 11, 22)), _cfg()) is None  # domenica
        # Lunedì 23/11: l'ultima scadenza è lunedì 16/11 e la finestra copre
        # anche sabato 14 e domenica 15.
        assert followup_window(_at(date(2026, 11, 23)), _cfg()) == (date(2026, 11, 10), date(2026, 11, 16))
        assert followup_window(FIXED_NOW, _cfg(REMINDER_FOLLOWUP_DAYS="8", REMINDER_FOLLOWUP_LOOKBACK_DAYS="2")) == (
            date(2026, 11, 8), date(2026, 11, 10))

    def test_window_uses_italian_date(self):
        # 23:30 UTC di martedì 17/11 = 00:30 italiane di mercoledì 18/11.
        late = datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)
        assert followup_window(late, _cfg())[1] == DUE7


class TestSyncGate:
    def test_no_sync_recorded(self, test_db_session):
        gate = sync_gate(test_db_session, FIXED_NOW, _cfg())
        assert gate["ok"] is False and "nessun sync" in gate["detail"] and gate["max_age_hours"] == 3

    def test_fresh_successful_sync_is_ok(self, test_db_session):
        _sync(test_db_session)
        gate = sync_gate(test_db_session, FIXED_NOW, _cfg())
        assert gate == {"ok": True, "max_age_hours": 3, "last_sync": "2026-11-18T09:45:00",
                        "age_minutes": 15, "detail": None}
        # Stesso esito con l'ora presa dall'orologio (nessun now passato).
        assert sync_gate(test_db_session, None, _cfg())["ok"] is True

    def test_failed_or_partial_sync_blocks(self, test_db_session):
        _sync(test_db_session, success=False)
        failed = sync_gate(test_db_session, FIXED_NOW, _cfg())
        assert failed["ok"] is False and "non è riuscito" in failed["detail"]
        assert "login" not in failed["detail"]  # nessun dettaglio dell'errore del sync
        _sync(test_db_session, partial=True)
        partial = sync_gate(test_db_session, FIXED_NOW, _cfg())
        assert partial["ok"] is False and "parziale" in partial["detail"]

    def test_stale_sync_blocks_at_the_threshold(self, test_db_session):
        _sync(test_db_session, minutes_ago=180)
        assert sync_gate(test_db_session, FIXED_NOW, _cfg())["ok"] is True
        _sync(test_db_session, minutes_ago=181)
        stale = sync_gate(test_db_session, FIXED_NOW, _cfg())
        assert stale["ok"] is False and "3 ore fa" in stale["detail"] and stale["age_minutes"] == 181
        assert sync_gate(test_db_session, FIXED_NOW, _cfg(REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS="5"))["ok"] is True

    def test_sync_time_in_the_future_is_not_trusted(self, test_db_session):
        _sync(test_db_session, minutes_ago=-4)   # qualche minuto di scarto tra orologi: ok
        assert sync_gate(test_db_session, FIXED_NOW, _cfg())["ok"] is True
        _sync(test_db_session, minutes_ago=-60)  # un'ora nel futuro: orologio sbagliato
        skew = sync_gate(test_db_session, FIXED_NOW, _cfg())
        assert skew["ok"] is False and "non è plausibile" in skew["detail"]

    def test_malformed_result_blocks(self, test_db_session):
        row = _sync(test_db_session)
        row.result = None
        test_db_session.commit()
        assert sync_gate(test_db_session, FIXED_NOW, _cfg())["ok"] is False
        row.result = {"fatturapro": "boh"}
        test_db_session.commit()
        assert sync_gate(test_db_session, FIXED_NOW, _cfg())["ok"] is False


# ── Selezione ─────────────────────────────────────────────────────────

class TestSelection:
    def test_only_unpaid_invoices_due_in_the_window(self, test_db_session):
        c = _cust(test_db_session)
        window = followup_window(FIXED_NOW, _cfg())
        keep = [
            _inv(test_db_session, c, "IN/7", 10.0, DUE7),                        # 7 giorni: entra oggi
            _inv(test_db_session, c, "IN/13", 10.0, date(2026, 11, 5)),          # 13 giorni: ultimo giorno
        ]
        _inv(test_db_session, c, "OUT/6", 10.0, date(2026, 11, 12))              # 6 giorni: non ancora
        _inv(test_db_session, c, "OUT/14", 10.0, date(2026, 11, 4))              # 14 giorni: fuori
        _inv(test_db_session, c, "OUT/PAID", 10.0, DUE7, status="paid")
        _inv(test_db_session, c, "OUT/VOID", 10.0, DUE7, status="void")
        _inv(test_db_session, c, "OUT/DISP", 10.0, DUE7, status="disputed")
        _inv(test_db_session, c, "OUT/ZERO", 10.0, DUE7, amount_due=0.0)
        _inv(test_db_session, None, "OUT/NOCUST", 10.0, DUE7)
        got = candidate_invoices(test_db_session, window)
        assert [i.invoice_number for i in got] == ["IN/13", "IN/7"]
        assert {i.id for i in got} == {i.id for i in keep}

    def test_needs_the_reminder_really_sent_to_the_customer(self, test_db_session):
        c = _cust(test_db_session)
        ok = _inv(test_db_session, c, "R/OK", 10.0, DUE7)
        row = _reminded(test_db_session, ok)
        assert reminder_sent(test_db_session, ok).id == row.id
        assert followup_extra_skip(test_db_session, ok, c) is None

        cases = {
            "R/NONE": None,
            "R/TEST": dict(mode="test"),
            "R/PROVA": dict(mode="prova", status="prepared"),
            "R/SKIP": dict(status="skipped"),
            "R/FAIL": dict(status="failed"),
            "R/SENDING": dict(status="sending"),
            "R/OTHERDUE": dict(due=date(2026, 11, 3)),  # promemoria per una scadenza poi cambiata
        }
        for num, kw in cases.items():
            inv = _inv(test_db_session, c, num, 10.0, DUE7)
            if kw is not None:
                _reminded(test_db_session, inv, **kw)
            assert reminder_sent(test_db_session, inv) is None, num
            assert followup_extra_skip(test_db_session, inv, c) == "senza_promemoria", num

    def test_waiting_customer_is_not_chased(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _due(test_db_session, c)
        assert followup_extra_skip(test_db_session, inv, c) == "in_attesa"
        for status in ("idle", "first_contact", "second_contact"):
            c.recovery_status = status
            assert followup_extra_skip(test_db_session, inv, c) is None, status

    def test_promised_invoice_is_not_chased(self, test_db_session):
        c = _cust(test_db_session)
        inv = _due(test_db_session, c, status="promised")
        assert followup_extra_skip(test_db_session, inv, c) == "promessa"
        assert followup_skip_reason(test_db_session, inv, c) == "promessa"
        for status in ("open", "contacted", "escalated"):
            inv.status = status
            assert followup_extra_skip(test_db_session, inv, c) is None, status

    def test_scope_comes_before_every_other_reason(self, test_db_session):
        # Senza promemoria la fattura è fuori perimetro, qualunque altro
        # motivo abbia (qui: cliente senza email e dall'avvocato).
        c = _cust(test_db_session, email="", recovery_status="lawyer")
        inv = _inv(test_db_session, c, "FUORI/1", 10.0, DUE7)
        assert followup_skip_reason(test_db_session, inv, c) == "senza_promemoria"
        _reminded(test_db_session, inv)
        assert followup_skip_reason(test_db_session, inv, c) == "avvocato"

    def test_other_overdue_invoices_are_only_the_certain_ones(self, test_db_session):
        c = _cust(test_db_session)
        trigger = _due(test_db_session, c)
        certain = _inv(test_db_session, c, "CERTA/1", 100.0, date(2026, 10, 10))
        maybe_paid = _inv(test_db_session, c, "FORSE/1", 200.0, date(2026, 10, 11), missing_streak=1)
        estimated = _inv(test_db_session, c, "STIMATA/1", 300.0, date(2026, 10, 12), due_date_source="assumed")
        promised = _inv(test_db_session, c, "PROMESSA/1", 400.0, date(2026, 10, 13), status="promised")
        cheque = _inv(test_db_session, c, "ASSEGNO/1", 500.0, date(2026, 10, 14), payment_pending="assegno")
        at_lawyer = _inv(test_db_session, c, "LEGALE/1", 600.0, date(2026, 9, 1))
        _inv(test_db_session, c, "PAGATA/1", 700.0, date(2026, 10, 15), status="paid")
        _inv(test_db_session, c, "FUTURA/1", 800.0, date(2026, 12, 1))
        case = RecoveryCase(customer_id=c.id, status="open")
        test_db_session.add(case)
        test_db_session.flush()
        handover = RecoveryAction(customer_id=c.id, case_id=case.id, action_type="lawyer", invoice_ids=[at_lawyer.id],
                                  completed_at=datetime(2026, 11, 1, 9, 0))
        test_db_session.add(handover)
        test_db_session.flush()
        test_db_session.add(RecoveryActionInvoice(action_id=handover.id, invoice_id=at_lawyer.id))
        test_db_session.commit()
        # Il promemoria (regola in produzione, invariata) le cita tutte tranne
        # l'assegno in incasso; il secondo avviso solo quella certa.
        base_nums = {i.invoice_number for i in re_mod.overdue_invoices(test_db_session, c.id, exclude_id=trigger.id)}
        assert base_nums == {"CERTA/1", "FORSE/1", "STIMATA/1", "PROMESSA/1", "LEGALE/1"}
        others = other_overdue_invoices(test_db_session, c.id, exclude_id=trigger.id)
        assert [i.id for i in others] == [certain.id]
        assert {maybe_paid.id, estimated.id, promised.id, cheque.id} & {i.id for i in others} == set()
        # Nell'email: una sola altra scaduta, totale = avviso + certa.
        _sync(test_db_session)
        mailer = FakeMailer()
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        text = mailer.sent[0]["text"]
        assert "1 altra fattura per 100,00 €. Totale da saldare: 526,76 €." in text
        summary_pdf = _pdf_strings(mailer.sent[0]["attachments"][1][1])
        assert "CERTA/1" in summary_pdf and "526,76 EUR" in summary_pdf
        for num in ("FORSE/1", "STIMATA/1", "PROMESSA/1", "ASSEGNO/1", "LEGALE/1", "PAGATA/1", "FUTURA/1"):
            assert num not in summary_pdf, num
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert (row.overdue_count, row.overdue_total) == (1, 100.0)
        # Anteprima e allegato rigenerato dicono lo stesso.
        entry = [e for e in preview_followups(test_db_session, cfg=_on_cfg()) if e["invoice_id"] == trigger.id][0]
        assert (entry["overdue_count"], entry["grand_total"]) == (1, 526.76)

    def test_no_other_certain_invoice_means_no_summary(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _inv(test_db_session, c, "FORSE/1", 200.0, date(2026, 10, 11), missing_streak=1)
        _sync(test_db_session)
        mailer = FakeMailer()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert [n for n, _ in mailer.sent[0]["attachments"]] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        assert "Risult" not in mailer.sent[0]["text"] and "Totale da saldare" not in mailer.sent[0]["text"]

    def test_common_exclusions_come_first(self, test_db_session):
        c = _cust(test_db_session, email="")
        inv = _inv(test_db_session, c, "NOEMAIL/1", 10.0, DUE7)  # senza promemoria E senza email
        assert re_mod.kind_skip_reason(test_db_session, inv, c, FOLLOWUP) == "senza_email"
        lawyer = _cust(test_db_session, nome="Dal Legale Srl", email="x@legale.it", recovery_status="lawyer",
                       partita_iva="01234567897")
        inv2 = _due(test_db_session, lawyer, num="AVV/1")
        assert re_mod.kind_skip_reason(test_db_session, inv2, lawyer, FOLLOWUP) == "avvocato"
        # Il promemoria NON ha le esclusioni del secondo avviso.
        plain = _inv(test_db_session, _cust(test_db_session, nome="Altro", email="a@altro.it", partita_iva="00000000000"),
                     "PLAIN/1", 10.0, DUE7)
        assert re_mod.kind_skip_reason(test_db_session, plain, plain.customer) is None
        assert re_mod.kind_skip_reason(test_db_session, plain, plain.customer, re_mod.PROMEMORIA) is None


# ── Composizione ──────────────────────────────────────────────────────

class TestCompose:
    def test_text_single_invoice(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        inv = _due(test_db_session, c)
        msg = compose_followup(c, inv, [], TODAY)
        assert msg["subject"] == "Sollecito di pagamento: fattura n. 2026/00001804/SAK scaduta il 11/11/2026 (426,76 €)"
        assert msg["text"] == (
            "Spett.le Akiro Milano Srl,\n\n"
            "abbiamo rilevato che la fattura n. 2026/00001804/SAK di 426,76 € è scaduta da 7 giorni "
            "(scadenza 11/11/2026) e non risulta ancora saldata. Copia della fattura in allegato.\n\n"
            "Le chiediamo di provvedere al saldo: bonifico a Sake Company srl, IBAN IT44N0200801671000105175151, "
            "causale \"Fattura 2026/00001804/SAK\".\n\n"
            "Se ha già effettuato il pagamento, Le chiediamo di inviarci la contabile rispondendo a questa email.\n\n"
            "Sake Company srl - Amministrazione"
        )
        assert msg["overdue_count"] == 0 and msg["grand_total"] == 426.76 and msg["days_overdue"] == 7
        assert "promemoria di cortesia" not in msg["text"] and "ignori" not in msg["text"]

    def test_text_with_other_overdue_invoices(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        inv = _due(test_db_session, c)
        old1 = _inv(test_db_session, c, "OLD/1", 493.68, date(2026, 10, 10))
        one = compose_followup(c, inv, [old1], TODAY)
        assert ("Risulta inoltre scaduta e non saldata 1 altra fattura per 493,68 €. "
                "Totale da saldare: 920,44 €. Riepilogo in allegato.") in one["text"]
        old2 = _inv(test_db_session, c, "OLD/2", 1000.0, date(2026, 9, 1))
        two = compose_followup(c, inv, [old1, old2], TODAY)
        assert ("Risultano inoltre scadute e non saldate altre 2 fatture per un totale di 1.493,68 €. "
                "Totale da saldare: 1.920,44 €. Riepilogo in allegato.") in two["text"]
        assert two["overdue_count"] == 2 and two["overdue_total"] == 1493.68 and two["grand_total"] == 1920.44

    def test_days_are_the_real_ones_not_the_stored_field(self, test_db_session):
        c = _cust(test_db_session)
        inv = _due(test_db_session, c, days_overdue=0)  # campo non ancora ricalcolato dal sync
        assert "è scaduta da 7 giorni" in compose_followup(c, inv, [], TODAY)["text"]
        assert "è scaduta da 9 giorni" in compose_followup(c, inv, [], TODAY + timedelta(days=2))["text"]
        assert "è scaduta da 1 giorno " in compose_followup(c, inv, [], DUE7 + timedelta(days=1))["text"]

    def test_html_is_escaped(self, test_db_session):
        c = _cust(test_db_session, nome="Fratelli <b>Rossi</b> & C.")
        inv = _due(test_db_session, c)
        body = compose_followup(c, inv, [], TODAY)["html"]
        assert "&lt;b&gt;Rossi&lt;/b&gt; &amp; C." in body and "<b>Rossi" not in body

    def test_attachments_names_and_pdf_content(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        inv = _due(test_db_session, c, days_overdue=0)  # il PDF non deve fidarsi del campo vecchio
        only = fu.build_followup_attachments(c, inv, [])
        assert [n for n, _ in only] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        copy = _pdf_strings(only[0][1])
        assert "COPIA DI CORTESIA" in copy and "Giorni di ritardo: 7" in copy

        old = _inv(test_db_session, c, "OLD/1", 493.68, date(2026, 10, 10), days_overdue=0)
        both = fu.build_followup_attachments(c, inv, [old])
        assert [n for n, _ in both] == [
            "copia_cortesia_fattura_2026_00001804_SAK.pdf", "riepilogo_scadute_Akiro_Milano_Srl.pdf"]
        summary = _pdf_strings(both[1][1])
        assert "Riepilogo Fatture Scadute" in summary and "in scadenza" not in summary
        assert "920,44 EUR" in summary  # totale: la fattura dell'avviso + l'altra scaduta
        # Giorni veri per entrambe: 7 (11/11) e 39 (10/10).
        assert re.search(r"\(7\)", summary) and re.search(r"\(39\)", summary)
        # I modelli veri non vengono toccati dalla vista per il PDF.
        assert inv.days_overdue == 0 and old.days_overdue == 0


# ── Il giro ───────────────────────────────────────────────────────────

class TestRun:
    def test_off_writes_and_sends_nothing(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _sync(test_db_session)
        mailer = FakeMailer()
        cfg = _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_x")  # promemoria reali, avviso spento
        summary = run_followups(test_db_session, mailer=mailer, cfg=cfg)
        assert summary["mode"] == MODE_OFF and summary["blocked"] == "off" and summary["candidates"] == 0
        assert "REMINDER_FOLLOWUP_ENABLED" in summary["missing_config"]
        assert mailer.calls == 0 and _rows(test_db_session) == []

    def test_real_send_goes_to_customer_with_copies(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl", email="amministrazione@akiro.it", tags="B2B, scotti")
        inv = _due(test_db_session, c)
        reminder_before = test_db_session.query(InvoiceReminderEmail).one()
        snapshot = (reminder_before.status, reminder_before.mode, reminder_before.subject, reminder_before.sent_at)
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert summary["mode"] == MODE_REALE and summary["blocked"] is None and summary["kind"] == "followup"
        assert (summary["candidates"], summary["sent"], summary["skipped"], summary["failed"]) == (1, 1, 0, 0)
        assert summary["window"] == ["2026-11-05", "2026-11-11"] and summary["sync"]["ok"] is True
        sent = mailer.sent[0]
        assert sent["to"] == ["amministrazione@akiro.it"]
        assert sent["cc"] == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]
        assert sent["sender"] == "Sake Company <fatture@sakecompany.com>" and sent["reply_to"] == "fatture@sakecompany.com"
        assert sent["subject"].startswith("Sollecito di pagamento: fattura n. 2026/00001804/SAK scaduta il 11/11/2026")
        assert "è scaduta da 7 giorni" in sent["text"] and "Le chiediamo di provvedere al saldo" in sent["text"]
        assert sent["idempotency_key"] == f"followup-{inv.id}-2026-11-11-reale"
        assert [n for n, _ in sent["attachments"]] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert (row.status, row.mode, row.to_email, row.due_date) == ("sent", "reale", "amministrazione@akiro.it", DUE7)
        assert row.cc_json == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]
        assert row.provider_message_id == "msg-1" and row.sent_at is not None and row.invoice_id == inv.id
        assert row.attachments_json == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        # Il registro dei promemoria non cambia di una virgola.
        after = test_db_session.query(InvoiceReminderEmail).one()
        assert (after.status, after.mode, after.subject, after.sent_at) == snapshot

    def test_summary_pdf_attached_when_other_invoices_are_overdue(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _inv(test_db_session, c, "OLD/1", 493.68, date(2026, 10, 10))
        _sync(test_db_session)
        mailer = FakeMailer()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert len(mailer.sent) == 1  # la vecchia scaduta è fuori finestra: nessun avviso suo
        assert [n for n, _ in mailer.sent[0]["attachments"]] == [
            "copia_cortesia_fattura_2026_00001804_SAK.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        assert "1 altra fattura per 493,68 €" in mailer.sent[0]["text"]
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert row.overdue_count == 1 and row.overdue_total == 493.68

    def test_exactly_once_same_day_and_following_days(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        mailer = FakeMailer()
        for day in (TODAY, TODAY, date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24)):
            _sync(test_db_session, now=_at(day))
            run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
        assert len(mailer.sent) == 1 and len(_rows(test_db_session)) == 1
        _sync(test_db_session)
        again = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert again["already"] == 1 and again["sent"] == 0

    def test_not_before_day_seven(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        mailer = FakeMailer()
        for day in (date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17)):  # +1 … +6
            _sync(test_db_session, now=_at(day))
            summary = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
            assert summary["candidates"] == 0, day
        assert mailer.calls == 0 and _rows(test_db_session) == []

    def test_weekend_never_sends_and_monday_catches_up_with_real_days(self, test_db_session):
        c = _cust(test_db_session)
        sat = _due(test_db_session, c, num="SAB/1", due=date(2026, 11, 14))
        sun = _due(test_db_session, c, num="DOM/1", due=date(2026, 11, 15))
        mon = _due(test_db_session, c, num="LUN/1", due=date(2026, 11, 16))
        mailer = FakeMailer()
        for day in (date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)):  # ven, sab, dom
            _sync(test_db_session, now=_at(day))
            summary = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
            assert summary["sent"] == 0, day
        assert run_followups(test_db_session, now_utc=_at(date(2026, 11, 21)), mailer=mailer, cfg=_on_cfg())["blocked"] == "weekend"
        assert mailer.calls == 0 and _rows(test_db_session) == []
        monday = _at(date(2026, 11, 23))
        _sync(test_db_session, now=monday)
        summary = run_followups(test_db_session, now_utc=monday, mailer=mailer, cfg=_on_cfg())
        assert summary["sent"] == 3
        texts = {s["subject"].split("n. ")[1].split(" scaduta")[0]: s["text"] for s in mailer.sent}
        assert "è scaduta da 9 giorni (scadenza 14/11/2026)" in texts["SAB/1"]
        assert "è scaduta da 8 giorni (scadenza 15/11/2026)" in texts["DOM/1"]
        assert "è scaduta da 7 giorni (scadenza 16/11/2026)" in texts["LUN/1"]
        assert {r.invoice_id for r in _rows(test_db_session)} == {sat.id, sun.id, mon.id}
        # Testo e PDF contano dallo STESSO giorno (quello del giro, non l'orologio).
        pdfs = {s["subject"].split("n. ")[1].split(" scaduta")[0]: _pdf_strings(s["attachments"][0][1]) for s in mailer.sent}
        assert "Giorni di ritardo: 9" in pdfs["SAB/1"] and "Giorni di ritardo: 7" in pdfs["LUN/1"]
        summary_pdf = _pdf_strings(mailer.sent[0]["attachments"][1][1])  # riepilogo: le tre scadute
        assert all(re.search(rf"\({n}\)", summary_pdf) for n in (9, 8, 7))

    def test_paid_invoice_gets_nothing(self, test_db_session):
        c = _cust(test_db_session)
        paid = _due(test_db_session, c, num="PAGATA/1", status="paid")
        settled = _due(test_db_session, c, num="SALDATA/1", amount_due=0.0)
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert summary["candidates"] == 0 and mailer.calls == 0 and _rows(test_db_session) == []
        assert paid.status == "paid" and settled.amount_due == 0.0

    def test_stale_sync_blocks_and_next_day_recovers(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        mailer = FakeMailer()
        _sync(test_db_session, minutes_ago=600)  # FatturaPro fermo da stanotte
        blocked = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert blocked["blocked"] == "sync" and blocked["sync"]["ok"] is False and blocked["candidates"] == 0
        assert mailer.calls == 0 and _rows(test_db_session) == []
        _sync(test_db_session, success=False)  # sync appena fatto, ma fallito
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["blocked"] == "sync"
        assert mailer.calls == 0 and _rows(test_db_session) == []
        # Il giorno dopo il sync è tornato: la finestra di recupero la riprende.
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        summary = run_followups(test_db_session, now_utc=next_day, mailer=mailer, cfg=_on_cfg())
        assert summary["sent"] == 1 and "è scaduta da 8 giorni" in mailer.sent[0]["text"]

    def test_window_end_is_the_last_chance(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        mailer = FakeMailer()
        last = _at(date(2026, 11, 24))   # 13 giorni dopo l'11/11: ultimo giorno utile
        beyond = _at(date(2026, 11, 25))  # 14 giorni: fuori
        _sync(test_db_session, now=beyond)
        assert run_followups(test_db_session, now_utc=beyond, mailer=mailer, cfg=_on_cfg())["candidates"] == 0
        _sync(test_db_session, now=last)
        assert run_followups(test_db_session, now_utc=last, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert "è scaduta da 13 giorni" in mailer.sent[0]["text"]

    def test_skips_are_recorded_and_reevaluated(self, test_db_session):
        c = _cust(test_db_session, email="")
        inv = _due(test_db_session, c)
        other = _inv(test_db_session, c, "NOPROM/1", 50.0, DUE7)  # senza promemoria
        _sync(test_db_session)
        mailer = FakeMailer()
        first = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (first["skipped"], first["out_of_scope"], first["candidates"]) == (1, 1, 1)
        assert first["sent"] == 0 and mailer.calls == 0
        reasons = {r.invoice_id: (r.status, r.skip_reason) for r in _rows(test_db_session)}
        assert reasons == {inv.id: ("skipped", "senza_email")}  # l'altra è fuori perimetro: nessuna riga
        # Il giorno dopo l'email c'è: la fattura col promemoria riceve l'avviso
        # (8 giorni); l'altra resta fuori dal registro.
        c.email = "amministrazione@shabu.it"
        test_db_session.commit()
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        second = run_followups(test_db_session, now_utc=next_day, mailer=mailer, cfg=_on_cfg())
        assert (second["sent"], second["skipped"], second["out_of_scope"]) == (1, 0, 1)
        assert mailer.sent[0]["to"] == ["amministrazione@shabu.it"] and "da 8 giorni" in mailer.sent[0]["text"]
        reasons = {r.invoice_id: (r.status, r.skip_reason) for r in _rows(test_db_session)}
        assert reasons == {inv.id: ("sent", None)}
        assert len(_rows(test_db_session)) == 1  # stessa riga, aggiornata
        assert test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=other.id).count() == 0

    def test_maybe_paid_in_collection_and_waiting_are_skipped(self, test_db_session):
        maybe = _due(test_db_session, _cust(test_db_session, nome="Forse Srl", email="a@forse.it", partita_iva="00000000001"),
                     num="FORSE/1", missing_streak=1)
        cheque = _due(test_db_session, _cust(test_db_session, nome="Assegno Srl", email="a@assegno.it", partita_iva="00000000002"),
                      num="ASSEGNO/1", payment_pending="assegno")
        waiting = _due(test_db_session, _cust(test_db_session, nome="Attesa Srl", email="a@attesa.it", partita_iva="00000000003",
                                              recovery_status="waiting"), num="ATTESA/1")
        promised = _due(test_db_session, _cust(test_db_session, nome="Promessa Srl", email="a@promessa.it", partita_iva="00000000009"),
                        num="PROMESSA/1", status="promised")
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert summary["skipped"] == 4 and mailer.calls == 0
        reasons = {r.invoice_id: r.skip_reason for r in _rows(test_db_session)}
        assert reasons == {maybe.id: "forse_pagata", cheque.id: "in_incasso", waiting.id: "in_attesa",
                           promised.id: "promessa"}

    def test_invoice_paid_while_the_round_is_running_gets_nothing(self, test_db_session):
        a = _due(test_db_session, _cust(test_db_session, nome="Alfa Srl", email="a@alfa.it", partita_iva="00000000005"), num="A/1")
        b = _due(test_db_session, _cust(test_db_session, nome="Beta Srl", email="b@beta.it", partita_iva="00000000006"), num="B/1")
        _sync(test_db_session)

        class PaysTheOtherOne(FakeMailer):
            """Mentre parte la prima email, il sync segna pagata la seconda."""
            def send(self, **kwargs):
                out = super().send(**kwargs)
                test_db_session.query(Invoice).filter(Invoice.id == b.id).update({"status": "paid", "amount_due": 0.0})
                return out

        mailer = PaysTheOtherOne()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["settled_meanwhile"], summary["candidates"]) == (1, 1, 1)
        assert [s["to"] for s in mailer.sent] == [["a@alfa.it"]]
        assert {r.invoice_id for r in _rows(test_db_session)} == {a.id}

    def test_test_mode_goes_to_test_address_then_real_resends_to_customer(self, test_db_session):
        c = _cust(test_db_session, tags="B2B, izzo")
        _due(test_db_session, c)
        _sync(test_db_session)
        mailer = FakeMailer()
        test_cfg = _on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        summary = run_followups(test_db_session, mailer=mailer, cfg=test_cfg)
        assert summary["mode"] == MODE_TEST and summary["sent"] == 1
        assert mailer.sent[0]["to"] == ["lorenzo@example.com"] and mailer.sent[0]["cc"] is None
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert (row.mode, row.to_email, row.original_to, row.cc_json) == (
            "test", "lorenzo@example.com", "amministrazione@shabu.it", [])
        # In test non si rimanda…
        assert run_followups(test_db_session, mailer=mailer, cfg=test_cfg)["sent"] == 0
        # …passando in reale, dentro la finestra, il cliente la riceve davvero.
        real = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert real["sent"] == 1 and mailer.sent[1]["to"] == ["amministrazione@shabu.it"]
        assert mailer.sent[1]["cc"] == ["a.izzo@sakecompany.com", "fatture@sakecompany.com"]
        assert mailer.sent[1]["idempotency_key"].endswith("-reale")
        assert test_db_session.query(InvoiceFollowupEmail).count() == 1

    def test_prova_mode_prepares_without_sending(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _sync(test_db_session)
        summary = run_followups(test_db_session, cfg=_cfg(REMINDER_FOLLOWUP_ENABLED=True))
        assert summary["mode"] == MODE_PROVA and summary["prepared"] == 1 and summary["sent"] == 0
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert row.status == "prepared" and row.subject.startswith("Sollecito di pagamento")

    def test_validation_failure_retries_alone_network_failure_waits(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _sync(test_db_session)
        refused = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        first = run_followups(test_db_session, mailer=refused, cfg=_on_cfg())
        assert first["failed"] == 1
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert (row.status, row.error_kind) == ("failed", "validation")
        assert run_followups(test_db_session, mailer=refused, cfg=_on_cfg())["sent"] == 1  # ritento automatico
        assert test_db_session.query(InvoiceFollowupEmail).one().status == "sent"

        c2 = _cust(test_db_session, nome="Rete Srl", email="a@rete.it", partita_iva="00000000004")
        _due(test_db_session, c2, num="RETE/1")
        timeout = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=timeout, cfg=_on_cfg())
        stuck = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c2.id).one()
        assert (stuck.status, stuck.error_kind) == ("failed", "network")
        again = run_followups(test_db_session, mailer=timeout, cfg=_on_cfg())  # stato incerto: non si ritenta da soli
        assert again["sent"] == 0 and again["already"] == 2 and timeout.calls == 1
        retried = retry_followup_row(test_db_session, stuck, mailer=timeout, cfg=_on_cfg())
        assert retried["sent"] == 1 and stuck.status == "sent"
        assert timeout.sent[0]["idempotency_key"] == f"followup-{stuck.invoice_id}-2026-11-11-reale"

    def test_one_broken_invoice_never_stops_the_round(self, test_db_session, monkeypatch):
        a = _due(test_db_session, _cust(test_db_session, nome="Alfa Srl", email="a@alfa.it", partita_iva="00000000005"), num="A/1")
        b = _due(test_db_session, _cust(test_db_session, nome="Beta Srl", email="b@beta.it", partita_iva="00000000006"), num="B/1")
        _sync(test_db_session)
        real = fu.compose_followup

        def flaky(customer, invoice, overdue, today=None):
            if invoice.id == a.id:
                raise RuntimeError("boom")
            return real(customer, invoice, overdue, today)

        monkeypatch.setattr(fu, "compose_followup", flaky)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert summary["failed"] == 1 and summary["sent"] == 1
        rows = {r.invoice_id: r for r in _rows(test_db_session)}
        assert rows[a.id].status == "failed" and rows[a.id].error_kind == "other" and "boom" in rows[a.id].error
        assert rows[b.id].status == "sent"
        # L'errore del secondo avviso sta nel SUO registro, non in quello dei promemoria.
        assert test_db_session.query(InvoiceReminderEmail).filter_by(status="failed").count() == 0

    def test_pdf_failure_is_recorded_and_retried(self, test_db_session, monkeypatch):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _sync(test_db_session)
        monkeypatch.setattr(fu, "build_followup_attachments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("font")))
        mailer = FakeMailer()
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["failed"] == 1
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert (row.status, row.error_kind) == ("failed", "pdf") and mailer.calls == 0
        monkeypatch.undo()
        for target in ("format", "reminder_email", "followup_email"):
            monkeypatch.setattr(f"backend.engine.{target}.datetime", _FixedDatetime)
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["sent"] == 1

    def test_busy_lock_and_mode_mismatch(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _sync(test_db_session)
        with pytest.raises(ModeMismatch):
            run_followups(test_db_session, mailer=FakeMailer(), cfg=_on_cfg(), expected_mode=MODE_TEST)
        with pytest.raises(ModeMismatch):  # l'operatore ha confermato «reale» ma è spento
            run_followups(test_db_session, mailer=FakeMailer(), cfg=_cfg(), expected_mode=MODE_REALE)
        assert re_mod._reminder_lock.acquire(blocking=False)
        try:
            with pytest.raises(ReminderBusy):
                run_followups(test_db_session, mailer=FakeMailer(), cfg=_on_cfg())
        finally:
            re_mod._reminder_lock.release()
        assert _rows(test_db_session) == []
        # Dopo, il lock è libero e il giro parte.
        assert run_followups(test_db_session, mailer=FakeMailer(), cfg=_on_cfg(), expected_mode=MODE_REALE)["sent"] == 1

    def test_recovery_workflow_is_untouched(self, test_db_session):
        c = _cust(test_db_session, recovery_status="first_contact")
        inv = _due(test_db_session, c)
        _sync(test_db_session)
        run_followups(test_db_session, mailer=FakeMailer(), cfg=_on_cfg())
        test_db_session.refresh(c)
        test_db_session.refresh(inv)
        assert c.recovery_status == "first_contact" and c.next_action_date is None
        assert inv.status == "open" and inv.case_id is None
        assert test_db_session.query(RecoveryAction).count() == 0

    def test_reminder_round_ignores_followup_rows(self, test_db_session):
        # I due giri convivono: stessa fattura, stessa scadenza, due registri.
        c = _cust(test_db_session)
        inv = _inv(test_db_session, c, "CONV/1", 100.0, date(2026, 11, 19))  # scade domani
        mailer = FakeMailer()
        cfg = _on_cfg()
        assert run_reminders(test_db_session, now_utc=FIXED_NOW, mailer=mailer, cfg=cfg)["sent"] == 1
        later = _at(date(2026, 11, 26))  # 7 giorni dopo la scadenza
        _sync(test_db_session, now=later)
        inv.days_overdue = 7
        test_db_session.commit()
        summary = run_followups(test_db_session, now_utc=later, mailer=mailer, cfg=cfg)
        assert summary["sent"] == 1
        assert [s["subject"].split(":")[0] for s in mailer.sent] == ["Promemoria", "Sollecito di pagamento"]
        assert test_db_session.query(InvoiceReminderEmail).count() == 1
        assert test_db_session.query(InvoiceFollowupEmail).count() == 1
        # Il promemoria di quella scadenza resta «già inviato».
        assert run_reminders(test_db_session, due_date=date(2026, 11, 19), mailer=mailer, cfg=cfg)["already"] == 1


# ── Anteprima ─────────────────────────────────────────────────────────

class TestPreview:
    def test_preview_writes_nothing_and_matches_the_run(self, test_db_session):
        send = _due(test_db_session, _cust(test_db_session, nome="Alfa Srl", email="a@alfa.it", partita_iva="00000000005",
                                           tags="B2B, maggioni"), num="A/1", amount=100.0)
        _inv(test_db_session, send.customer, "A/OLD", 40.0, date(2026, 10, 1))
        noprom = _inv(test_db_session, _cust(test_db_session, nome="Beta Srl", email="b@beta.it", partita_iva="00000000006"),
                      "B/1", 20.0, date(2026, 11, 9))
        waiting = _due(test_db_session, _cust(test_db_session, nome="Gamma Srl", email="g@gamma.it", partita_iva="00000000007",
                                              recovery_status="waiting"), num="G/1", due=date(2026, 11, 6))
        _sync(test_db_session)
        cfg = _on_cfg()
        entries = preview_followups(test_db_session, cfg=cfg)
        assert _rows(test_db_session) == []
        by_num = {e["invoice_number"]: e for e in entries}
        assert [e["invoice_number"] for e in entries] == ["G/1", "B/1", "A/1"]  # per scadenza
        a = by_num["A/1"]
        assert a["will_process"] is True and a["why"] == "nuova" and a["days_overdue"] == 7 and a["skip_reason"] is None
        assert a["cc"] == ["g.maggioni@sakecompany.com", "fatture@sakecompany.com"]
        assert (a["overdue_count"], a["overdue_total"], a["grand_total"]) == (1, 40.0, 140.0)
        assert a["subject"].startswith("Sollecito di pagamento") and "è scaduta da 7 giorni" in a["text"]
        assert a["reminder_sent_at"] == "2026-11-10T08:15:00" and a["due_date"] == "2026-11-11"
        assert (by_num["B/1"]["skip_reason"], by_num["B/1"]["will_process"], by_num["B/1"]["text"]) == (
            "senza_promemoria", False, None)
        assert by_num["B/1"]["skip_label"] == "Promemoria pre-scadenza non inviato al cliente"
        assert (by_num["G/1"]["skip_reason"], by_num["G/1"]["days_overdue"]) == ("in_attesa", 12)
        # Quello che l'anteprima promette è quello che il giro invia.
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=cfg)
        promised = {e["invoice_id"] for e in entries if e["will_process"]}
        assert {r.invoice_id for r in _rows(test_db_session) if r.status == "sent"} == promised == {send.id}
        assert (summary["sent"], summary["skipped"], summary["out_of_scope"]) == (1, 1, 1)
        assert {r.invoice_id for r in _rows(test_db_session) if r.status == "skipped"} == {waiting.id}
        assert test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=noprom.id).count() == 0
        after = {e["invoice_number"]: e for e in preview_followups(test_db_session, cfg=cfg)}
        assert (after["A/1"]["already"], after["A/1"]["will_process"], after["A/1"]["why"]) == ("sent", False, "già inviata")

    def test_preview_when_off_shows_what_would_be_sent(self, test_db_session):
        c = _cust(test_db_session, tags="B2B, scotti")
        _due(test_db_session, c)
        off = _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_x")
        entry = preview_followups(test_db_session, cfg=off)[0]
        assert entry["will_process"] is True and entry["cc"] == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]
        assert _rows(test_db_session) == []

    def test_preview_empty_in_weekend(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c, due=date(2026, 11, 14))
        assert preview_followups(test_db_session, now_utc=_at(date(2026, 11, 21)), cfg=_on_cfg()) == []


# ── Ritento e invio di prova ──────────────────────────────────────────

class TestRetryAndTestSend:
    def _stuck(self, s):
        c = _cust(s)
        inv = _due(s, c)
        _sync(s)
        run_followups(s, mailer=FakeMailer(fail_times=1, kind=ERROR_NETWORK), cfg=_on_cfg())
        return inv, s.query(InvoiceFollowupEmail).one()

    def test_retry_refusals(self, test_db_session):
        inv, row = self._stuck(test_db_session)
        mailer = FakeMailer()
        with pytest.raises(ValueError, match="spento"):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_x"))
        with pytest.raises(ModeMismatch):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg(), expected_mode=MODE_TEST)
        _sync(test_db_session, minutes_ago=600)
        with pytest.raises(ValueError, match="sync fatture non aggiornato"):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())
        _sync(test_db_session)
        inv.due_date = date(2026, 11, 30)  # scadenza spostata dopo l'avviso
        test_db_session.commit()
        with pytest.raises(ValueError, match="scadenza della fattura è cambiata"):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())
        inv.due_date = DUE7
        inv.status = "paid"
        test_db_session.commit()
        with pytest.raises(ValueError, match="non più da sollecitare"):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 0 and row.status == "failed"

    def test_retry_refuses_missing_or_not_overdue_invoice(self, test_db_session):
        inv, row = self._stuck(test_db_session)
        mailer = FakeMailer()
        before_due = _at(date(2026, 11, 11))  # il giorno della scadenza: non è ancora scaduta
        _sync(test_db_session, now=before_due)
        with pytest.raises(ValueError, match="non è scaduta"):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg(), now_utc=before_due)
        _sync(test_db_session)
        ghost = InvoiceFollowupEmail(invoice_id=987654, due_date=DUE7, status="failed", mode="reale", error_kind="network")
        with pytest.raises(ValueError, match="non più presente"):
            retry_followup_row(test_db_session, ghost, mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 0

    def test_retry_of_an_orphan_sending_row(self, test_db_session):
        # Crash a metà invio: la riga resta `sending`. Il giro non la tocca,
        # l'operatore la ritenta a mano (stessa Idempotency-Key).
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        _sync(test_db_session)
        orphan = InvoiceFollowupEmail(invoice_id=inv.id, customer_id=c.id, due_date=DUE7, status="sending", mode="reale")
        test_db_session.add(orphan)
        test_db_session.commit()
        mailer = FakeMailer()
        untouched = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert untouched["already"] == 1 and mailer.calls == 0 and orphan.status == "sending"
        retried = retry_followup_row(test_db_session, orphan, mailer=mailer, cfg=_on_cfg())
        assert retried["sent"] == 1 and orphan.status == "sent"
        assert mailer.sent[0]["idempotency_key"] == f"followup-{inv.id}-2026-11-11-reale"
        assert test_db_session.query(InvoiceFollowupEmail).count() == 1

    def test_retry_only_stuck_rows_and_busy(self, test_db_session):
        inv, row = self._stuck(test_db_session)
        assert re_mod._reminder_lock.acquire(blocking=False)
        try:
            with pytest.raises(ReminderBusy):
                retry_followup_row(test_db_session, row, mailer=FakeMailer(), cfg=_on_cfg())
        finally:
            re_mod._reminder_lock.release()
        mailer = FakeMailer()
        assert retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        with pytest.raises(ValueError, match="falliti o interrotti"):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 1

    def test_test_send_works_when_off_and_never_touches_the_registry(self, test_db_session):
        c = _cust(test_db_session, tags="B2B, scotti")
        inv = _due(test_db_session, c)
        mailer = FakeMailer()
        off = _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_x")
        out = send_followup_test(test_db_session, inv.id, " Lorenzo@Example.com ", mailer=mailer, cfg=off)
        assert out["sent_to"] == "lorenzo@example.com" and out["subject"].startswith("[PROVA] Sollecito di pagamento")
        assert out["would_cc"] == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]
        sent = mailer.sent[0]
        assert sent["to"] == ["lorenzo@example.com"] and "cc" not in sent and "è scaduta da 7 giorni" in sent["text"]
        assert [n for n, _ in sent["attachments"]] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        assert _rows(test_db_session) == []

    def test_test_send_cites_only_certain_overdue_invoices(self, test_db_session):
        # La prova deve mostrare ESATTAMENTE l'email che partirebbe: stesse
        # altre scadute (solo le certe), stesso totale, stessi allegati.
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        _inv(test_db_session, c, "CERTA/1", 100.0, date(2026, 10, 10))
        _inv(test_db_session, c, "FORSE/1", 200.0, date(2026, 10, 11), missing_streak=1)
        _inv(test_db_session, c, "STIMATA/1", 300.0, date(2026, 10, 12), due_date_source="assumed")
        mailer = FakeMailer()
        send_followup_test(test_db_session, inv.id, "lorenzo@example.com", mailer=mailer, cfg=_on_cfg())
        sent = mailer.sent[0]
        assert "1 altra fattura per 100,00 €. Totale da saldare: 526,76 €." in sent["text"]
        summary_pdf = _pdf_strings(sent["attachments"][1][1])
        assert "CERTA/1" in summary_pdf and "FORSE/1" not in summary_pdf and "STIMATA/1" not in summary_pdf
        # E coincide con l'anteprima.
        entry = [e for e in preview_followups(test_db_session, cfg=_on_cfg()) if e["invoice_id"] == inv.id][0]
        assert entry["text"] == sent["text"]

    def test_test_send_refusals(self, test_db_session):
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        cfg = _on_cfg()
        with pytest.raises(NotFound):
            send_followup_test(test_db_session, 999999, "lorenzo@example.com", mailer=FakeMailer(), cfg=cfg)
        with pytest.raises(ValueError, match="email di un cliente"):
            send_followup_test(test_db_session, inv.id, "amministrazione@shabu.it", mailer=FakeMailer(), cfg=cfg)
        with pytest.raises(ValueError, match="non valido"):
            send_followup_test(test_db_session, inv.id, "non-una-email", mailer=FakeMailer(), cfg=cfg)
        future = _inv(test_db_session, c, "FUT/1", 10.0, date(2026, 11, 25))
        with pytest.raises(ValueError, match="non ancora scaduta"):
            send_followup_test(test_db_session, future.id, "lorenzo@example.com", mailer=FakeMailer(), cfg=cfg)
        today_due = _inv(test_db_session, c, "OGGI/1", 10.0, TODAY)
        with pytest.raises(ValueError, match="non ancora scaduta"):
            send_followup_test(test_db_session, today_due.id, "lorenzo@example.com", mailer=FakeMailer(), cfg=cfg)
        noprom = _inv(test_db_session, c, "NOPROM/1", 10.0, DUE7)
        with pytest.raises(ValueError, match="Promemoria pre-scadenza non inviato"):
            send_followup_test(test_db_session, noprom.id, "lorenzo@example.com", mailer=FakeMailer(), cfg=cfg)
        with pytest.raises(ValueError, match="RESEND_API_KEY"):
            send_followup_test(test_db_session, inv.id, "lorenzo@example.com", mailer=FakeMailer(), cfg=_cfg())


# ── API ───────────────────────────────────────────────────────────────

class TestApi:
    def _enable(self, monkeypatch, enabled=True, key="re_x", test_to=""):
        from backend.config import config as real_config
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_ENABLED", True)
        monkeypatch.setattr(real_config, "RESEND_API_KEY", key)
        monkeypatch.setattr(real_config, "REMINDER_EMAIL_TEST_TO", test_to)
        monkeypatch.setattr(real_config, "REMINDER_FOLLOWUP_ENABLED", enabled)
        monkeypatch.setattr(real_config, "REMINDER_FOLLOWUP_DAYS", "7")
        monkeypatch.setattr(real_config, "REMINDER_FOLLOWUP_LOOKBACK_DAYS", "6")
        monkeypatch.setattr(real_config, "REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS", "3")

    def _spy(self, monkeypatch, **mailer_kw):
        created = []

        class Spy(FakeMailer):
            def __init__(self, api_key, api_url=None):
                super().__init__(**mailer_kw)
                created.append(self)

        monkeypatch.setattr("backend.connectors.resend_mail.ResendMailer", Spy)
        return created

    def test_status_when_off(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch, enabled=False)
        c = _cust(test_db_session, tags="B2B, scotti")
        inv = _due(test_db_session, c)
        _sync(test_db_session)
        data = test_client.get("/api/reminders/followup").json()
        assert data["enabled"] is False and data["mode"] == "off" and data["mode_label"] == "Spento (solo anteprima)"
        assert data["send_mode"] == "reale" and data["missing_config"] == ["REMINDER_FOLLOWUP_ENABLED"]
        assert (data["days_after_due"], data["lookback_days"]) == (7, 6)
        assert data["window"] == ["2026-11-05", "2026-11-11"] and data["weekend"] is False
        assert data["sync"]["ok"] is True and data["schedule"].startswith("09:15")
        assert data["cc_effective"] == ["fatture@sakecompany.com"] and data["recent"] == []
        entry = data["preview"][0]
        assert entry["invoice_id"] == inv.id and entry["will_process"] is True and entry["days_overdue"] == 7
        # Da spento il pulsante non invia: lo dice, senza scrivere nulla.
        created = self._spy(monkeypatch)
        run = test_client.post("/api/reminders/followup/run", json={"expected_mode": "off"})
        assert run.status_code == 200 and run.json()["blocked"] == "off" and run.json()["sent"] == 0
        assert created == [] and _rows(test_db_session) == []
        wrong = test_client.post("/api/reminders/followup/run", json={"expected_mode": "reale"})
        assert wrong.status_code == 409 and "Spento" in wrong.json()["detail"]

    def test_run_log_detail_attachments_when_on(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session, tags="B2B, scotti")
        inv = _due(test_db_session, c)
        _inv(test_db_session, c, "OLD/1", 493.68, date(2026, 10, 10))
        _sync(test_db_session)
        created = self._spy(monkeypatch)
        status = test_client.get("/api/reminders/followup").json()
        assert status["enabled"] is True and status["mode"] == "reale"
        run = test_client.post("/api/reminders/followup/run", json={"expected_mode": "reale"})
        assert run.status_code == 200 and run.json()["sent"] == 1 and run.json()["blocked"] is None
        assert created[0].sent[0]["to"] == ["amministrazione@shabu.it"] and created[0].closed
        assert created[0].sent[0]["cc"] == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]

        recent = test_client.get("/api/reminders/followup").json()["recent"]
        assert len(recent) == 1
        item = recent[0]
        assert (item["kind"], item["status"], item["invoice_id"], item["agents"]) == ("followup", "sent", inv.id, ["Scotti"])
        assert item["attachments"] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        detail = test_client.get(f"/api/reminders/followup/{item['id']}")
        assert detail.status_code == 200 and "è scaduta da 7 giorni" in detail.json()["body_text"]
        for index in (0, 1):
            pdf = test_client.get(f"/api/reminders/followup/{item['id']}/attachments/{index}")
            assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        assert "riepilogo_scadute" in test_client.get(
            f"/api/reminders/followup/{item['id']}/attachments/1").headers["content-disposition"]
        assert test_client.get(f"/api/reminders/followup/{item['id']}/attachments/5").status_code == 404
        assert test_client.get("/api/reminders/followup/999999").status_code == 404
        assert test_client.post("/api/reminders/followup/999999/retry").status_code == 404
        assert test_client.post(f"/api/reminders/followup/{item['id']}/retry").status_code == 409  # non è ferma
        # Gli endpoint del promemoria non vedono le righe del secondo avviso.
        assert test_client.get("/api/reminders/email").json()["recent"][0]["kind"] == "promemoria"
        assert len(test_client.get("/api/reminders/email").json()["recent"]) == 1

    def test_regenerated_attachment_uses_only_certain_overdue_invoices(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _inv(test_db_session, c, "CERTA/1", 100.0, date(2026, 10, 10))
        _inv(test_db_session, c, "FORSE/1", 200.0, date(2026, 10, 11), missing_streak=1)
        _sync(test_db_session)
        self._spy(monkeypatch)
        assert test_client.post("/api/reminders/followup/run").json()["sent"] == 1
        row = test_db_session.query(InvoiceFollowupEmail).one()
        pdf = test_client.get(f"/api/reminders/followup/{row.id}/attachments/1")
        assert pdf.status_code == 200
        text = _pdf_strings(pdf.content)
        assert "CERTA/1" in text and "FORSE/1" not in text and "526,76 EUR" in text
        # Se l'unica altra scaduta diventa incerta, il riepilogo non esiste più.
        test_db_session.query(Invoice).filter(Invoice.invoice_number == "CERTA/1").update({"missing_streak": 1})
        test_db_session.commit()
        assert test_client.get(f"/api/reminders/followup/{row.id}/attachments/1").status_code == 404
        assert test_client.get(f"/api/reminders/followup/{row.id}/attachments/0").status_code == 200

    def test_customer_log_merges_reminders_and_followups(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _sync(test_db_session)
        self._spy(monkeypatch)
        assert test_client.post("/api/reminders/followup/run").json()["sent"] == 1
        other = _cust(test_db_session, nome="Altro Srl", email="a@altro.it", partita_iva="00000000008")
        _due(test_db_session, other, num="ALTRO/1", due=date(2026, 11, 30))  # solo promemoria
        log = test_client.get("/api/reminders/email/log", params={"customer_id": c.id}).json()
        assert log["count"] == 2 and {i["kind"] for i in log["items"]} == {"promemoria", "followup"}
        assert log["items"][0]["kind"] == "followup"  # il più recente in cima
        assert all(i["customer_id"] == c.id for i in log["items"])
        everyone = test_client.get("/api/reminders/email/log", params={"limit": 2}).json()
        assert everyone["count"] == 2
        # Limite più piccolo del totale (3 righe: 2 promemoria + 1 secondo
        # avviso): i due registri sono fusi per data, dal più recente, e il
        # taglio tiene le prime. Qui l'ordine di creazione è: promemoria del
        # cliente, secondo avviso, promemoria di «Altro».
        allrows = test_client.get("/api/reminders/email/log").json()
        assert allrows["count"] == 3
        assert [(i["kind"], i["invoice_number"]) for i in allrows["items"]] == [
            ("promemoria", "ALTRO/1"), ("followup", "2026/00001804/SAK - Fattura"),
            ("promemoria", "2026/00001804/SAK - Fattura")]
        stamps = [i["created_at"] for i in allrows["items"]]
        assert stamps == sorted(stamps, reverse=True)
        one = test_client.get("/api/reminders/email/log", params={"limit": 1}).json()
        assert one["count"] == 1 and one["items"][0]["invoice_number"] == "ALTRO/1"
        assert [(i["kind"], i["id"]) for i in everyone["items"]] == [(i["kind"], i["id"]) for i in allrows["items"][:2]]

    def test_blocked_by_sync_and_weekend(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        _due(test_db_session, c)
        created = self._spy(monkeypatch)
        _sync(test_db_session, success=False)
        status = test_client.get("/api/reminders/followup").json()
        assert status["sync"]["ok"] is False and status["preview"][0]["will_process"] is True
        run = test_client.post("/api/reminders/followup/run").json()
        assert run["blocked"] == "sync" and run["blocked_label"].startswith("Sync fatture non aggiornato")
        assert created == [] and _rows(test_db_session) == []
        monkeypatch.setattr("backend.engine.followup_email.followup_window", lambda *a, **k: None)
        weekend = test_client.get("/api/reminders/followup").json()
        assert weekend["weekend"] is True and weekend["window"] is None and weekend["preview"] == []
        assert test_client.post("/api/reminders/followup/run").json()["blocked"] == "weekend"

    def test_test_send_endpoint(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch, enabled=False)
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        created = self._spy(monkeypatch)
        ok = test_client.post("/api/reminders/followup/test", json={"invoice_id": inv.id, "to": "lorenzo@ef-ti.com"})
        assert ok.status_code == 200 and ok.json()["sent_to"] == "lorenzo@ef-ti.com"
        assert created[0].sent[0]["subject"].startswith("[PROVA] Sollecito di pagamento")
        assert test_client.post("/api/reminders/followup/test", json={"invoice_id": 999999, "to": "lorenzo@ef-ti.com"}).status_code == 404
        refused = test_client.post("/api/reminders/followup/test", json={"invoice_id": inv.id, "to": "amministrazione@shabu.it"})
        assert refused.status_code == 409 and "cliente" in refused.json()["detail"]
        self._spy(monkeypatch, fail_times=1, kind=ERROR_VALIDATION)
        failed = test_client.post("/api/reminders/followup/test", json={"invoice_id": inv.id, "to": "lorenzo@ef-ti.com"})
        assert failed.status_code == 502 and "Resend HTTP 422" in failed.json()["detail"]
        assert _rows(test_db_session) == []

    def test_retry_endpoint_and_busy(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _sync(test_db_session)
        self._spy(monkeypatch, fail_times=1, kind=ERROR_NETWORK)
        assert test_client.post("/api/reminders/followup/run").json()["failed"] == 1
        row = test_db_session.query(InvoiceFollowupEmail).one()
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert item["needs_manual_retry"] is True and item["error_kind"] == "network"
        assert re_mod._reminder_lock.acquire(blocking=False)
        try:
            assert test_client.post("/api/reminders/followup/run").status_code == 409
            assert test_client.post(f"/api/reminders/followup/{row.id}/retry").status_code == 409
        finally:
            re_mod._reminder_lock.release()
        self._spy(monkeypatch)
        retried = test_client.post(f"/api/reminders/followup/{row.id}/retry", json={"expected_mode": "reale"})
        assert retried.status_code == 200 and retried.json()["sent"] == 1


# ── Job schedulato e modello ──────────────────────────────────────────

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
        monkeypatch.setattr("backend.engine.followup_email.run_followups",
                            lambda session: calls.append(session) or {"sent": 0, "items": [], "blocked": "off"})
        assert sched.run_followup_email_job() == {"sent": 0, "items": [], "blocked": "off"}
        assert calls == [sess] and sess.closed

    def test_job_never_raises_and_reports_busy(self, monkeypatch):
        from backend import scheduler as sched
        monkeypatch.setattr("backend.database.get_session_direct", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("backend.engine.followup_email.run_followups",
                            lambda session: (_ for _ in ()).throw(RuntimeError("db giù")))
        assert "error" in sched.run_followup_email_job()
        monkeypatch.setattr("backend.engine.followup_email.run_followups",
                            lambda session: (_ for _ in ()).throw(ReminderBusy()))
        assert sched.run_followup_email_job() == {"skipped": "busy"}

    def test_morning_jobs_run_in_order_and_in_isolation(self, monkeypatch):
        from backend import scheduler as sched
        order = []
        monkeypatch.setattr("backend.database.get_session_direct", lambda: SimpleNamespace(close=lambda: None))

        def broken_reminders(session):
            order.append("reminder")
            raise RuntimeError("promemoria giù")

        monkeypatch.setattr("backend.engine.reminder_email.run_reminders", broken_reminders)
        monkeypatch.setattr("backend.engine.followup_email.run_followups",
                            lambda session: order.append("followup") or {"sent": 2, "items": []})
        out = sched.run_email_jobs()
        assert order == ["reminder", "followup"]
        assert "error" in out["reminder"] and out["followup"] == {"sent": 2, "items": []}

    def test_scheduled_job_is_the_morning_pair(self, monkeypatch):
        from backend import scheduler as sched
        added = {}

        class FakeScheduler:
            def __init__(self, **kw):
                pass

            def add_job(self, func, **kw):
                added[kw["id"]] = func

            def start(self):
                pass

        monkeypatch.setattr(sched, "BackgroundScheduler", FakeScheduler)
        monkeypatch.setattr(sched, "_scheduler_started", False)
        monkeypatch.setattr(sched, "_scheduler", None)
        sched.start_scheduler()
        try:
            assert added["reminder_email_job"] is sched.run_email_jobs
            assert added["reminder_email_catchup"] is sched.run_reminder_email_catchup
        finally:
            monkeypatch.setattr(sched, "_scheduler_started", False)
            monkeypatch.setattr(sched, "_scheduler", None)


class TestModel:
    def test_same_columns_as_the_reminder_registry(self):
        # Lo stesso motore scrive entrambi i registri: le colonne non devono divergere.
        cols = lambda model: {c.name: (str(c.type), c.nullable) for c in model.__table__.columns}  # noqa: E731
        assert cols(InvoiceFollowupEmail) == cols(InvoiceReminderEmail)
        assert InvoiceFollowupEmail.__tablename__ == "invoice_followup_emails"

    def test_one_row_per_invoice_and_due_date(self, test_db_session):
        from sqlalchemy.exc import IntegrityError
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        test_db_session.add(InvoiceFollowupEmail(invoice_id=inv.id, due_date=DUE7, status="sent", mode="reale"))
        test_db_session.commit()
        test_db_session.add(InvoiceFollowupEmail(invoice_id=inv.id, due_date=DUE7, status="sending", mode="reale"))
        with pytest.raises(IntegrityError):
            test_db_session.commit()
        test_db_session.rollback()
