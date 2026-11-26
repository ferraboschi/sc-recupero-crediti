"""Secondo avviso email: fattura scaduta da N giorni e ancora non saldata
(backend/engine/followup_email.py, API /api/reminders/followup…, job del mattino).

Richiesta owner 24/11/2026: una settimana dopo la scadenza, se la fattura del
promemoria risulta ancora non pagata, parte «un'ulteriore email» più diretta:
abbiamo rilevato una fattura scaduta da 7 giorni, chiediamo il saldo.
E ancora (24/11/2026): «Non si devono ricevere due mail. In caso di scadenze
prossime unire le mail e adeguare il testo. Scadenza prossima significa ± un
giorno» → UNA email per cliente (TestOneEmailPerCustomer).
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
    MODE_OFF, NEAR_DUE_DAYS, by_customer, candidate_invoices, compose_followup, email_group_of, followup_days,
    followup_extra_skip, followup_lookback, followup_skip_reason, followup_window, group_key, group_members,
    max_sync_age_hours, merge_until, other_overdue_invoices, plan_customer, preview_followups, reminder_sent,
    resolve_followup_mode, retry_followup_row, row_invoices, run_followups, send_followup_test, sync_gate,
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
        self.keys = []   # la Idempotency-Key di ogni tentativo, riuscito o no
        self.fail_times = fail_times
        self.kind = kind
        self.calls = 0
        self.closed = False

    def send(self, **kwargs):
        self.calls += 1
        self.keys.append(kwargs.get("idempotency_key"))
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


def _idem(customer, invoices, day=TODAY, mode="reale"):
    """L'inizio della Idempotency-Key di un PRIMO tentativo per quell'email
    (poi viene l'istante del tentativo: ogni tentativo nuovo ha la sua)."""
    return f"followup-{group_key(customer.id, invoices, day)}-{mode}-"


def _other(s, n, nome=None, **kw):
    """Un altro cliente, con email e P.IVA sue."""
    nome = nome or f"Cliente {n} Srl"
    return _cust(s, nome=nome, email=kw.pop("email", f"amministrazione@cliente{n}.it"),
                 partita_iva=kw.pop("partita_iva", f"{n:011d}"), **kw)


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

    def test_near_due_date_means_one_day(self):
        # «Scadenza prossima significa ± un giorno» (owner, 24/11/2026).
        assert NEAR_DUE_DAYS == 1
        assert merge_until(followup_window(FIXED_NOW, _cfg())) == date(2026, 11, 12)
        assert merge_until((date(2026, 11, 10), date(2026, 11, 16))) == date(2026, 11, 17)

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
        near = _inv(test_db_session, c, "NEAR/6", 10.0, date(2026, 11, 12))      # 6 giorni: solo da UNIRE
        _inv(test_db_session, c, "OUT/5", 10.0, date(2026, 11, 13))              # 5 giorni: non ancora
        _inv(test_db_session, c, "OUT/14", 10.0, date(2026, 11, 4))              # 14 giorni: fuori
        _inv(test_db_session, c, "OUT/PAID", 10.0, DUE7, status="paid")
        _inv(test_db_session, c, "OUT/VOID", 10.0, DUE7, status="void")
        _inv(test_db_session, c, "OUT/DISP", 10.0, DUE7, status="disputed")
        _inv(test_db_session, c, "OUT/ZERO", 10.0, DUE7, amount_due=0.0)
        _inv(test_db_session, None, "OUT/NOCUST", 10.0, DUE7)
        got = candidate_invoices(test_db_session, window)
        assert [i.invoice_number for i in got] == ["IN/13", "IN/7", "NEAR/6"]
        assert {i.id for i in got} == {i.id for i in keep} | {near.id}
        # Solo le fatture di un cliente (invio di prova).
        other = _other(test_db_session, 2)
        mine = _inv(test_db_session, other, "ALTRO/7", 10.0, DUE7)
        assert [i.id for i in candidate_invoices(test_db_session, window, customer_id=other.id)] == [mine.id]
        assert len(candidate_invoices(test_db_session, window)) == 4
        # Raggruppate per cliente, dal cliente con la scadenza più vecchia.
        groups = by_customer(candidate_invoices(test_db_session, window))
        assert [(cid, [i.invoice_number for i in g]) for cid, g in groups] == [
            (c.id, ["IN/13", "IN/7", "NEAR/6"]), (other.id, ["ALTRO/7"])]

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

    def test_reminder_must_have_gone_to_the_same_customer(self, test_db_session):
        # Fattura riabbinata a un altro cliente dopo il promemoria: chi non ha
        # ricevuto il primo avviso non riceve il secondo.
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        assert reminder_sent(test_db_session, inv) is not None
        other = _other(test_db_session, 2)
        inv.customer_id, inv.customer_name_raw, inv.customer_piva_raw = other.id, other.ragione_sociale, other.partita_iva
        test_db_session.commit()
        assert reminder_sent(test_db_session, inv) is None
        assert followup_skip_reason(test_db_session, inv, other) == "senza_promemoria"
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["out_of_scope"]) == (0, 1) and mailer.calls == 0 and _rows(test_db_session) == []
        inv.customer_id = None
        assert reminder_sent(test_db_session, inv) is None

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
        others = other_overdue_invoices(test_db_session, c.id, exclude_ids=[trigger.id])
        assert [i.id for i in others] == [certain.id]
        # Senza esclusioni la fattura dell'avviso è una scaduta certa come le altre;
        # escludendole entrambe non resta nulla.
        assert {i.id for i in other_overdue_invoices(test_db_session, c.id)} == {certain.id, trigger.id}
        assert other_overdue_invoices(test_db_session, c.id, exclude_ids=[trigger.id, certain.id]) == []
        assert {maybe_paid.id, estimated.id, promised.id, cheque.id} & {i.id for i in others} == set()
        # L'anteprima dice lo stesso: una sola altra scaduta, totale = avviso + certa.
        entry = [e for e in preview_followups(test_db_session, cfg=_on_cfg()) if e["invoice_id"] == trigger.id][0]
        assert (entry["overdue_count"], entry["overdue_total"], entry["grand_total"]) == (1, 100.0, 526.76)
        # Nell'email: una sola altra scaduta, totale = avviso + certa.
        _sync(test_db_session)
        mailer = FakeMailer()
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert entry["text"] == mailer.sent[0]["text"]
        text = mailer.sent[0]["text"]
        assert "un'altra fattura per 100,00 €. Totale da saldare: 526,76 €." in text
        summary_pdf = _pdf_strings(mailer.sent[0]["attachments"][1][1])
        assert "CERTA/1" in summary_pdf and "526,76 EUR" in summary_pdf
        for num in ("FORSE/1", "STIMATA/1", "PROMESSA/1", "ASSEGNO/1", "LEGALE/1", "PAGATA/1", "FUTURA/1"):
            assert num not in summary_pdf, num
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert (row.overdue_count, row.overdue_total) == (1, 100.0)

    def test_no_other_certain_invoice_means_no_summary(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c)
        _inv(test_db_session, c, "FORSE/1", 200.0, date(2026, 10, 11), missing_streak=1)
        _sync(test_db_session)
        mailer = FakeMailer()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert [n for n, _ in mailer.sent[0]["attachments"]] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        assert "Risult" not in mailer.sent[0]["text"] and "Totale da saldare" not in mailer.sent[0]["text"]

    def test_common_exclusions_come_first_and_test_send_ignores_the_address(self, test_db_session):
        # Le esclusioni del promemoria valgono prima di quelle proprie.
        lawyer = _other(test_db_session, 2, nome="Dal Legale Srl", recovery_status="lawyer")
        inv = _due(test_db_session, lawyer, num="AVV/1", status="promised")
        assert followup_skip_reason(test_db_session, inv, lawyer) == "avvocato"
        cheque = _due(test_db_session, _other(test_db_session, 3), num="ASS/1", status="promised",
                      payment_pending="assegno")
        assert followup_skip_reason(test_db_session, cheque, cheque.customer) == "in_incasso"
        # Cliente senza email: saltata nel giro; l'invio di PROVA (che va a un
        # indirizzo interno) non ha bisogno dell'indirizzo del cliente…
        noemail = _other(test_db_session, 4, email="")
        inv2 = _due(test_db_session, noemail, num="NOEMAIL/1")
        assert followup_skip_reason(test_db_session, inv2, noemail) == "senza_email"
        assert followup_skip_reason(test_db_session, inv2, noemail, ignore_email=True) is None
        bad = _other(test_db_session, 5, email="non-una-email")
        inv3 = _due(test_db_session, bad, num="BAD/1")
        assert followup_skip_reason(test_db_session, inv3, bad) == "email_non_valida"
        assert followup_skip_reason(test_db_session, inv3, bad, ignore_email=True) is None
        # …ma ogni altro motivo resta, anche in prova.
        noemail.recovery_status = "waiting"
        assert followup_skip_reason(test_db_session, inv2, noemail, ignore_email=True) == "in_attesa"
        noemail.recovery_status = "lawyer"
        assert followup_skip_reason(test_db_session, inv2, noemail, ignore_email=True) == "avvocato"
        # Il promemoria in produzione NON ha le esclusioni del secondo avviso.
        waiting = _other(test_db_session, 6, recovery_status="waiting")
        plain = _inv(test_db_session, waiting, "PLAIN/1", 10.0, DUE7, status="promised")
        assert re_mod.skip_reason(plain, waiting) is None


# ── Composizione ──────────────────────────────────────────────────────

class TestCompose:
    def test_text_single_invoice(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        inv = _due(test_db_session, c)
        msg = compose_followup(c, [inv], [], TODAY)
        assert msg["subject"] == "Sollecito di pagamento: fattura n. 2026/00001804/SAK scaduta da 7 giorni (426,76 €)"
        assert msg["text"] == (
            "Spett.le Akiro Milano Srl,\n\n"
            "abbiamo rilevato che la fattura n. 2026/00001804/SAK di 426,76 € è scaduta da 7 giorni "
            "(scadenza 11/11/2026) e non risulta ancora saldata. Copia della fattura in allegato.\n\n"
            "Le chiediamo di provvedere al saldo: bonifico a Sake Company srl, IBAN IT44N0200801671000105175151, "
            "causale \"Fattura 2026/00001804/SAK\".\n\n"
            "Se ha già effettuato il pagamento, Le chiediamo di inviarci la contabile rispondendo a questa email.\n\n"
            "Sake Company srl - Amministrazione"
        )
        assert msg["overdue_count"] == 0 and msg["grand_total"] == 426.76
        assert (msg["invoice_count"], msg["invoices_total"]) == (1, 426.76)
        assert msg["lines"] == [{"invoice_id": inv.id, "number": "2026/00001804/SAK", "amount_due": 426.76,
                                 "due_date": "2026-11-11", "days_overdue": 7}]
        with pytest.raises(ValueError, match="nessuna fattura"):
            compose_followup(c, [], [], TODAY)
        assert "promemoria di cortesia" not in msg["text"] and "ignori" not in msg["text"]

    def test_text_with_other_overdue_invoices(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        inv = _due(test_db_session, c)
        old1 = _inv(test_db_session, c, "OLD/1", 493.68, date(2026, 10, 10))
        one = compose_followup(c, [inv], [old1], TODAY)
        assert ("Risulta inoltre scaduta e non saldata un'altra fattura per 493,68 €. "
                "Totale da saldare: 920,44 €. Riepilogo in allegato.") in one["text"]
        old2 = _inv(test_db_session, c, "OLD/2", 1000.0, date(2026, 9, 1))
        two = compose_followup(c, [inv], [old1, old2], TODAY)
        assert ("Risultano inoltre scadute e non saldate altre 2 fatture per un totale di 1.493,68 €. "
                "Totale da saldare: 1.920,44 €. Riepilogo in allegato.") in two["text"]
        assert two["overdue_count"] == 2 and two["overdue_total"] == 1493.68 and two["grand_total"] == 1920.44

    def test_days_are_the_real_ones_not_the_stored_field(self, test_db_session):
        c = _cust(test_db_session)
        inv = _due(test_db_session, c, days_overdue=0)  # campo non ancora ricalcolato dal sync
        assert "è scaduta da 7 giorni" in compose_followup(c, [inv], [], TODAY)["text"]
        assert "è scaduta da 9 giorni" in compose_followup(c, [inv], [], TODAY + timedelta(days=2))["text"]
        assert "è scaduta da 1 giorno " in compose_followup(c, [inv], [], DUE7 + timedelta(days=1))["text"]

    def test_html_is_escaped(self, test_db_session):
        c = _cust(test_db_session, nome="Fratelli <b>Rossi</b> & C.")
        inv = _due(test_db_session, c)
        body = compose_followup(c, [inv], [], TODAY)["html"]
        assert "&lt;b&gt;Rossi&lt;/b&gt; &amp; C." in body and "<b>Rossi" not in body

    def test_attachments_names_and_pdf_content(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        inv = _due(test_db_session, c, days_overdue=0)  # il PDF non deve fidarsi del campo vecchio
        only = fu.build_followup_attachments(c, [inv], [])
        assert [n for n, _ in only] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        copy = _pdf_strings(only[0][1])
        assert "COPIA DI CORTESIA" in copy and "Giorni di ritardo: 7" in copy

        old = _inv(test_db_session, c, "OLD/1", 493.68, date(2026, 10, 10), days_overdue=0)
        both = fu.build_followup_attachments(c, [inv], [old])
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
        assert (summary["invoices"], summary["merged_early"]) == (1, 0)
        assert summary["window"] == ["2026-11-05", "2026-11-11"] and summary["sync"]["ok"] is True
        assert summary["merge_until"] == "2026-11-12"
        sent = mailer.sent[0]
        assert sent["to"] == ["amministrazione@akiro.it"]
        assert sent["cc"] == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]
        assert sent["sender"] == "Sake Company <fatture@sakecompany.com>" and sent["reply_to"] == "fatture@sakecompany.com"
        assert sent["subject"] == "Sollecito di pagamento: fattura n. 2026/00001804/SAK scaduta da 7 giorni (426,76 €)"
        assert "è scaduta da 7 giorni" in sent["text"] and "Le chiediamo di provvedere al saldo" in sent["text"]
        assert sent["idempotency_key"].startswith(_idem(c, [inv])) and len(sent["idempotency_key"]) < 100
        assert [n for n, _ in sent["attachments"]] == ["copia_cortesia_fattura_2026_00001804_SAK.pdf"]
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert row.group_key == group_key(c.id, [inv], TODAY) and row.idempotency_key == sent["idempotency_key"]
        assert summary["items"][0]["group_key"] == row.group_key and summary["items"][0]["group_size"] == 1
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
        assert "un'altra fattura per 493,68 €" in mailer.sent[0]["text"]
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
        sat = _due(test_db_session, _other(test_db_session, 1, nome="Sabato Srl"), num="SAB/1", due=date(2026, 11, 14))
        sun = _due(test_db_session, _other(test_db_session, 2, nome="Domenica Srl"), num="DOM/1", due=date(2026, 11, 15))
        mon = _due(test_db_session, _other(test_db_session, 3, nome="Lunedi Srl"), num="LUN/1", due=date(2026, 11, 16))
        mailer = FakeMailer()
        for day in (date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)):  # ven, sab, dom
            _sync(test_db_session, now=_at(day))
            summary = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
            assert summary["sent"] == 0 and summary["candidates"] == 0, day
        assert run_followups(test_db_session, now_utc=_at(date(2026, 11, 21)), mailer=mailer, cfg=_on_cfg())["blocked"] == "weekend"
        assert mailer.calls == 0 and _rows(test_db_session) == []
        monday = _at(date(2026, 11, 23))
        _sync(test_db_session, now=monday)
        summary = run_followups(test_db_session, now_utc=monday, mailer=mailer, cfg=_on_cfg())
        assert summary["sent"] == 3 and summary["invoices"] == 3
        texts = {s["subject"].split("n. ")[1].split(" scaduta")[0]: s["text"] for s in mailer.sent}
        assert "è scaduta da 9 giorni (scadenza 14/11/2026)" in texts["SAB/1"]
        assert "è scaduta da 8 giorni (scadenza 15/11/2026)" in texts["DOM/1"]
        assert "è scaduta da 7 giorni (scadenza 16/11/2026)" in texts["LUN/1"]
        assert {r.invoice_id for r in _rows(test_db_session)} == {sat.id, sun.id, mon.id}
        # Testo e PDF contano dallo STESSO giorno (quello del giro, non l'orologio).
        pdfs = {s["subject"].split("n. ")[1].split(" scaduta")[0]: _pdf_strings(s["attachments"][0][1]) for s in mailer.sent}
        assert "Giorni di ritardo: 9" in pdfs["SAB/1"] and "Giorni di ritardo: 7" in pdfs["LUN/1"]

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
        assert "-reale-" in mailer.sent[1]["idempotency_key"] and "-test-" in mailer.sent[0]["idempotency_key"]
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
        rete = test_db_session.query(Invoice).filter(Invoice.id == stuck.invoice_id).one()
        # Il ritento continua il tentativo incerto: STESSA chiave verso Resend.
        assert timeout.sent[0]["idempotency_key"] == timeout.keys[0] and timeout.keys[0].startswith(_idem(c2, [rete]))

    def test_one_broken_invoice_never_stops_the_round(self, test_db_session, monkeypatch):
        a = _due(test_db_session, _cust(test_db_session, nome="Alfa Srl", email="a@alfa.it", partita_iva="00000000005"), num="A/1")
        b = _due(test_db_session, _cust(test_db_session, nome="Beta Srl", email="b@beta.it", partita_iva="00000000006"), num="B/1")
        _sync(test_db_session)
        real = fu.compose_followup

        def flaky(customer, invoices, overdue, today=None):
            if invoices[0].id == a.id:
                raise RuntimeError("boom")
            return real(customer, invoices, overdue, today)

        monkeypatch.setattr(fu, "compose_followup", flaky)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert summary["failed"] == 1 and summary["sent"] == 1
        rows = {r.invoice_id: r for r in _rows(test_db_session)}
        # Errore PRIMA di qualunque invio: visibile, e il giro la riprova da solo.
        assert rows[a.id].status == "failed" and rows[a.id].error_kind == "not_attempted" and "boom" in rows[a.id].error
        assert "nulla è partito" in rows[a.id].error and rows[a.id].idempotency_key is None
        assert rows[b.id].status == "sent"
        monkeypatch.setattr(fu, "compose_followup", real)
        again = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (again["sent"], again["failed"]) == (1, 0) and rows[a.id].status == "sent"
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


# ── Una email per cliente ─────────────────────────────────────────────

N1 = "2026/00001804/SAK - Fattura"
N2 = "2026/00001812/SAK - Fattura"
DUE6 = date(2026, 11, 12)   # scaduta da 6 giorni: «scadenza prossima» a DUE7
COPY1 = "copia_cortesia_fattura_2026_00001804_SAK.pdf"
COPY2 = "copia_cortesia_fattura_2026_00001812_SAK.pdf"


class TestOneEmailPerCustomer:
    """Owner 24/11/2026: «Non si devono ricevere due mail. In caso di scadenze
    prossime unire le mail e adeguare il testo. Scadenza prossima significa ±
    un giorno.»"""

    # ── Testo e allegati ──

    def test_text_for_two_invoices(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        a = _due(test_db_session, c)
        b = _due(test_db_session, c, num=N2, amount=493.68, due=DUE6)
        msg = compose_followup(c, [b, a], [], TODAY)  # l'ordine in ingresso non conta
        assert msg["subject"] == "Sollecito di pagamento: 2 fatture scadute (920,44 €)"
        assert msg["text"] == (
            "Spett.le Akiro Milano Srl,\n\n"
            "abbiamo rilevato che le seguenti fatture sono scadute e non risultano ancora saldate:\n"
            "- n. 2026/00001804/SAK di 426,76 €, scaduta da 7 giorni (scadenza 11/11/2026)\n"
            "- n. 2026/00001812/SAK di 493,68 €, scaduta da 6 giorni (scadenza 12/11/2026)\n\n"
            "Totale: 920,44 €. Copie delle fatture in allegato.\n\n"
            "Le chiediamo di provvedere al saldo: bonifico a Sake Company srl, IBAN IT44N0200801671000105175151, "
            "causale \"Fatture 2026/00001804/SAK e 2026/00001812/SAK\".\n\n"
            "Se ha già effettuato il pagamento, Le chiediamo di inviarci la contabile rispondendo a questa email.\n\n"
            "Sake Company srl - Amministrazione"
        )
        assert (msg["invoice_count"], msg["invoices_total"], msg["overdue_count"], msg["grand_total"]) == (
            2, 920.44, 0, 920.44)
        assert [(line["invoice_id"], line["days_overdue"]) for line in msg["lines"]] == [(a.id, 7), (b.id, 6)]
        # HTML: l'elenco va a capo riga per riga, dentro lo stesso paragrafo.
        assert ("saldate:<br>- n. 2026/00001804/SAK di 426,76 €, scaduta da 7 giorni (scadenza 11/11/2026)"
                "<br>- n. 2026/00001812/SAK di 493,68 €") in msg["html"]
        assert "è scaduta da" not in msg["text"]  # il testo a una fattura non c'entra più

    def test_text_for_several_invoices_other_overdue_and_causale(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        a = _due(test_db_session, c, num="A/1", amount=100.0)
        b = _due(test_db_session, c, num="B/2", amount=200.0)
        d = _due(test_db_session, c, num="C/3", amount=300.0, due=date(2026, 11, 10))
        old = _inv(test_db_session, c, "OLD/1", 50.0, date(2026, 10, 1))
        three = compose_followup(c, [a, b, d], [old], TODAY)
        assert three["subject"] == "Sollecito di pagamento: 3 fatture scadute (600,00 €)"
        assert (
            "saldate:\n"
            "- n. C/3 di 300,00 €, scaduta da 8 giorni (scadenza 10/11/2026)\n"
            "- n. A/1 di 100,00 €, scaduta da 7 giorni (scadenza 11/11/2026)\n"
            "- n. B/2 di 200,00 €, scaduta da 7 giorni (scadenza 11/11/2026)\n\n"
            "Totale di queste fatture: 600,00 €. Copie delle fatture in allegato.\n\n"
            "Risulta inoltre scaduta e non saldata un'altra fattura per 50,00 €. "
            "Totale da saldare: 650,00 €. Riepilogo in allegato.\n\n"
            "Le chiediamo di provvedere al saldo"
        ) in three["text"]
        assert 'causale "Fatture C/3, A/1 e B/2".' in three["text"]
        assert (three["invoices_total"], three["overdue_count"], three["overdue_total"], three["grand_total"]) == (
            600.0, 1, 50.0, 650.0)
        old2 = _inv(test_db_session, c, "OLD/2", 25.0, date(2026, 10, 2))
        assert ("Risultano inoltre scadute e non saldate altre 2 fatture per un totale di 75,00 €. "
                "Totale da saldare: 675,00 €.") in compose_followup(c, [a, b, d], [old, old2], TODAY)["text"]
        # Oltre tre fatture la causale non le elenca più (140 caratteri SEPA).
        e = _due(test_db_session, c, num="D/4", amount=400.0, due=date(2026, 11, 9))
        four = compose_followup(c, [a, b, d, e], [], TODAY)
        assert four["subject"] == "Sollecito di pagamento: 4 fatture scadute (1.000,00 €)"
        assert "IBAN IT44N0200801671000105175151, indicando nella causale i numeri delle fatture." in four["text"]
        assert 'causale "' not in four["text"]
        assert "\n\nTotale: 1.000,00 €. Copie delle fatture in allegato.\n\n" in four["text"]
        assert "- n. D/4 di 400,00 €, scaduta da 9 giorni (scadenza 09/11/2026)\n- n. C/3" in four["text"]

    def test_attachments_one_copy_per_invoice_plus_summary(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        a = _due(test_db_session, c, days_overdue=0)   # i PDF non si fidano del campo vecchio
        b = _due(test_db_session, c, num=N2, amount=493.68, due=DUE6, days_overdue=0)
        only = fu.build_followup_attachments(c, [b, a], [], TODAY)
        assert [n for n, _ in only] == [COPY1, COPY2]
        assert "Giorni di ritardo: 7" in _pdf_strings(only[0][1]) and "Giorni di ritardo: 6" in _pdf_strings(only[1][1])
        assert all(content[:4] == b"%PDF" for _, content in only)
        old = _inv(test_db_session, c, "OLD/1", 100.0, date(2026, 10, 10), days_overdue=0)
        full = fu.build_followup_attachments(c, [a, b], [old], TODAY)
        assert [n for n, _ in full] == [COPY1, COPY2, "riepilogo_scadute_Akiro_Milano_Srl.pdf"]
        summary = _pdf_strings(full[2][1])
        # Riepilogo: le due dell'email + l'altra, coi giorni veri e il totale di tutte.
        assert "1.020,44 EUR" in summary and "OLD/1" in summary and "2026/00001812/SAK" in summary
        assert all(re.search(rf"\({n}\)", summary) for n in (7, 6, 39))

    def test_group_key_is_the_identity_of_one_email(self, test_db_session):
        c = _cust(test_db_session)
        a = _due(test_db_session, c)
        b = _due(test_db_session, c, num=N2)
        key = group_key(c.id, [a, b], TODAY)
        assert key == group_key(c.id, [b, a], TODAY)  # l'ordine non conta
        assert key.startswith(f"c{c.id}-20261118-") and len(key) == len(f"c{c.id}-20261118-") + 16
        others = {
            group_key(c.id, [a], TODAY), group_key(c.id, [b], TODAY),
            group_key(c.id, [a, b], TODAY + timedelta(days=1)),   # altro giorno: altro testo
            group_key(c.id + 1, [a, b], TODAY),
        }
        assert key not in others and len(others) == 4
        b.due_date = DUE6  # scadenza cambiata: non è più la stessa email
        assert group_key(c.id, [a, b], TODAY) != key

    # ── Il giro: scadenze uguali, a un giorno, più lontane ──

    def test_same_due_date_one_email(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl", email="amministrazione@akiro.it", tags="B2B, scotti")
        a = _due(test_db_session, c)
        b = _due(test_db_session, c, num=N2, amount=493.68)
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["candidates"], summary["merged_early"]) == (1, 2, 2, 0)
        assert (summary["failed"], summary["skipped"], summary["already"]) == (0, 0, 0)
        assert mailer.calls == 1
        sent = mailer.sent[0]
        assert sent["to"] == ["amministrazione@akiro.it"]
        assert sent["cc"] == ["g.scotti@sakecompany.com", "fatture@sakecompany.com"]
        assert sent["subject"] == "Sollecito di pagamento: 2 fatture scadute (920,44 €)"
        assert "- n. 2026/00001804/SAK di 426,76 €, scaduta da 7 giorni" in sent["text"]
        assert "- n. 2026/00001812/SAK di 493,68 €, scaduta da 7 giorni" in sent["text"]
        assert [n for n, _ in sent["attachments"]] == [COPY1, COPY2]
        assert sent["idempotency_key"].startswith(_idem(c, [a, b])) and _idem(c, [a, b]) == _idem(c, [b, a])
        rows = {r.invoice_id: r for r in _rows(test_db_session)}
        assert set(rows) == {a.id, b.id}
        assert {r.status for r in rows.values()} == {"sent"} and {r.mode for r in rows.values()} == {"reale"}
        assert {r.group_key for r in rows.values()} == {group_key(c.id, [a, b], TODAY)}
        assert {r.provider_message_id for r in rows.values()} == {"msg-1"}
        assert {r.subject for r in rows.values()} == {sent["subject"]}
        assert {r.body_text for r in rows.values()} == {sent["text"]}
        assert (rows[a.id].amount_due, rows[b.id].amount_due) == (426.76, 493.68)  # ognuna il suo importo
        assert all(r.attachments_json == [COPY1, COPY2] for r in rows.values())
        assert all(r.to_email == "amministrazione@akiro.it" and r.sent_at is not None for r in rows.values())
        assert sorted((i["invoice_id"], i["group_size"]) for i in summary["items"]) == sorted([(a.id, 2), (b.id, 2)])
        # Un secondo giro non rimanda nulla.
        again = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (again["sent"], again["already"], again["candidates"]) == (0, 2, 2) and mailer.calls == 1

    def test_due_dates_one_day_apart_one_email_with_the_real_days(self, test_db_session):
        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="A/1", amount=100.0)             # 11/11: 7 giorni
        b = _due(test_db_session, c, num="B/2", amount=200.0, due=DUE6)   # 12/11: 6 giorni → unita
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["candidates"], summary["merged_early"]) == (1, 2, 2, 1)
        assert mailer.calls == 1
        assert ("- n. A/1 di 100,00 €, scaduta da 7 giorni (scadenza 11/11/2026)\n"
                "- n. B/2 di 200,00 €, scaduta da 6 giorni (scadenza 12/11/2026)") in mailer.sent[0]["text"]
        assert mailer.sent[0]["subject"] == "Sollecito di pagamento: 2 fatture scadute (300,00 €)"
        assert {r.invoice_id: (r.status, r.due_date) for r in _rows(test_db_session)} == {
            a.id: ("sent", DUE7), b.id: ("sent", DUE6)}
        # Il giorno dopo (quando B compie 7 giorni) e in tutti i successivi: nessuna seconda email.
        for day in (date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)):
            _sync(test_db_session, now=_at(day))
            later = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
            assert (later["sent"], later["prepared"], later["failed"]) == (0, 0, 0), day
        assert mailer.calls == 1 and len(_rows(test_db_session)) == 2

    def test_a_near_invoice_alone_waits_for_its_day(self, test_db_session):
        c = _cust(test_db_session)
        b = _due(test_db_session, c, num="B/2", due=DUE6)  # 6 giorni e nessun'altra: oggi nulla
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert [summary[k] for k in ("candidates", "sent", "invoices", "merged_early", "skipped", "already",
                                     "out_of_scope", "settled_meanwhile")] == [0] * 8
        assert mailer.calls == 0 and _rows(test_db_session) == []
        assert preview_followups(test_db_session, cfg=_on_cfg()) == []
        plan = plan_customer(test_db_session, [b], followup_window(FIXED_NOW, _cfg()), MODE_REALE, TODAY)
        assert plan.email == [] and plan.in_scope == 0 and plan.early_ids == set()
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        assert run_followups(test_db_session, now_utc=next_day, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert "è scaduta da 7 giorni (scadenza 12/11/2026)" in mailer.sent[0]["text"]

    def test_due_dates_two_days_apart_stay_two_emails_on_two_days(self, test_db_session):
        # «Prossima» è ± UN giorno: 11/11 e 13/11 non si uniscono.
        c = _cust(test_db_session)
        _due(test_db_session, c, num="A/1", amount=100.0)
        _due(test_db_session, c, num="B/2", amount=200.0, due=date(2026, 11, 13))
        mailer = FakeMailer()
        sent_by_day = {}
        for day in (TODAY, date(2026, 11, 19), date(2026, 11, 20)):
            _sync(test_db_session, now=_at(day))
            sent_by_day[day] = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())["sent"]
        assert sent_by_day == {TODAY: 1, date(2026, 11, 19): 0, date(2026, 11, 20): 1}
        assert [s["subject"] for s in mailer.sent] == [
            "Sollecito di pagamento: fattura n. A/1 scaduta da 7 giorni (100,00 €)",
            "Sollecito di pagamento: fattura n. B/2 scaduta da 7 giorni (200,00 €)"]
        assert "è scaduta da 7 giorni" in mailer.sent[1]["text"]
        # La seconda cita la prima tra le «altre scadute» (è ancora non saldata).
        assert "un'altra fattura per 100,00 €. Totale da saldare: 300,00 €." in mailer.sent[1]["text"]

    def test_three_consecutive_due_dates(self, test_db_session):
        # 10, 11 e 12/11: il 17 partono insieme le prime due (7 e 6 giorni);
        # la terza, da sola, il 19 (7 giorni). Nessuna email il 18.
        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="A/1", due=date(2026, 11, 10))
        b = _due(test_db_session, c, num="B/2", due=date(2026, 11, 11))
        d = _due(test_db_session, c, num="C/3", due=date(2026, 11, 12))
        mailer = FakeMailer()
        out = {}
        for day in (date(2026, 11, 17), TODAY, date(2026, 11, 19)):
            _sync(test_db_session, now=_at(day))
            out[day] = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
        assert [out[day]["sent"] for day in out] == [1, 0, 1]
        assert (out[date(2026, 11, 17)]["invoices"], out[date(2026, 11, 17)]["merged_early"]) == (2, 1)
        assert out[TODAY]["already"] == 2 and out[TODAY]["candidates"] == 2
        assert "2 fatture scadute" in mailer.sent[0]["subject"] and "fattura n. C/3" in mailer.sent[1]["subject"]
        keys = {r.invoice_id: r.group_key for r in _rows(test_db_session)}
        assert keys[a.id] == keys[b.id] != keys[d.id]

    def test_first_round_puts_everything_due_in_one_email_per_customer(self, test_db_session):
        # Prima accensione (o giro saltato): nella finestra ci sono più
        # scadenze dello stesso cliente. Una sola email, non una per scadenza.
        c = _cust(test_db_session)
        _due(test_db_session, c, num="A/1", amount=10.0, due=date(2026, 11, 5))    # 13 giorni
        _due(test_db_session, c, num="B/2", amount=20.0, due=date(2026, 11, 9))    # 9 giorni
        _due(test_db_session, c, num="C/3", amount=30.0, due=DUE7)                 # 7 giorni
        _due(test_db_session, c, num="D/4", amount=40.0, due=DUE6)                 # 6 giorni: unita
        other = _other(test_db_session, 2)
        _due(test_db_session, other, num="X/1", amount=5.0, due=date(2026, 11, 6))
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["candidates"], summary["merged_early"]) == (2, 5, 5, 1)
        by_to = {s["to"][0]: s for s in mailer.sent}
        assert set(by_to) == {"amministrazione@shabu.it", "amministrazione@cliente2.it"}
        mine = by_to["amministrazione@shabu.it"]
        assert mine["subject"] == "Sollecito di pagamento: 4 fatture scadute (100,00 €)"
        assert ("- n. A/1 di 10,00 €, scaduta da 13 giorni (scadenza 05/11/2026)\n"
                "- n. B/2 di 20,00 €, scaduta da 9 giorni (scadenza 09/11/2026)\n"
                "- n. C/3 di 30,00 €, scaduta da 7 giorni (scadenza 11/11/2026)\n"
                "- n. D/4 di 40,00 €, scaduta da 6 giorni (scadenza 12/11/2026)") in mine["text"]
        assert len(mine["attachments"]) == 4
        assert "fattura n. X/1 scaduta da 12 giorni (5,00 €)" in by_to["amministrazione@cliente2.it"]["subject"]

    def test_friday_takes_the_saturday_due_date_along(self, test_db_session):
        c = _cust(test_db_session)
        _due(test_db_session, c, num="VEN/1", due=date(2026, 11, 13))
        _due(test_db_session, c, num="SAB/1", due=date(2026, 11, 14))
        mailer = FakeMailer()
        friday, monday = _at(date(2026, 11, 20)), _at(date(2026, 11, 23))
        _sync(test_db_session, now=friday)
        summary = run_followups(test_db_session, now_utc=friday, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["merged_early"]) == (1, 2, 1)
        assert ("- n. VEN/1 di 426,76 €, scaduta da 7 giorni (scadenza 13/11/2026)\n"
                "- n. SAB/1 di 426,76 €, scaduta da 6 giorni (scadenza 14/11/2026)") in mailer.sent[0]["text"]
        _sync(test_db_session, now=monday)
        assert run_followups(test_db_session, now_utc=monday, mailer=mailer, cfg=_on_cfg())["sent"] == 0
        assert mailer.calls == 1

    def test_monday_merges_weekend_monday_and_tuesday(self, test_db_session):
        c = _cust(test_db_session)
        for num, day in (("SAB/1", 14), ("DOM/1", 15), ("LUN/1", 16), ("MAR/1", 17)):
            _due(test_db_session, c, num=num, amount=10.0, due=date(2026, 11, day))
        mailer = FakeMailer()
        monday = _at(date(2026, 11, 23))
        _sync(test_db_session, now=monday)
        summary = run_followups(test_db_session, now_utc=monday, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["merged_early"]) == (1, 4, 1)
        text = mailer.sent[0]["text"]
        assert [text.count(f"scaduta da {n} giorni") for n in (9, 8, 7, 6)] == [1, 1, 1, 1]
        pdf_days = [_pdf_strings(content) for _, content in mailer.sent[0]["attachments"]]
        assert ["Giorni di ritardo: 9" in pdf_days[0], "Giorni di ritardo: 6" in pdf_days[3]] == [True, True]

    def test_never_merges_across_customers(self, test_db_session):
        # Due schede cliente diverse (due ragioni sociali) restano due email,
        # anche con lo stesso indirizzo: una email non mescola due aziende.
        one = _other(test_db_session, 1, nome="Uno Srl", email="amministrazione@gruppo.it")
        two = _other(test_db_session, 2, nome="Due Srl", email="amministrazione@gruppo.it")
        a = _due(test_db_session, one, num="UNO/1")
        b = _due(test_db_session, two, num="DUE/1")
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"]) == (2, 2)
        assert sorted(s["text"].split(",")[0] for s in mailer.sent) == ["Spett.le Due Srl", "Spett.le Uno Srl"]
        keys = {r.invoice_id: r.group_key for r in _rows(test_db_session)}
        assert keys[a.id] != keys[b.id]

    # ── Chi entra nell'email ──

    def test_only_ready_invoices_enter_the_email(self, test_db_session):
        c = _cust(test_db_session)
        ok = _due(test_db_session, c, num="OK/1", amount=100.0)
        noprom = _inv(test_db_session, c, "NOPROM/1", 50.0, DUE7)                       # mai avuto il promemoria
        promised = _due(test_db_session, c, num="PROM/1", amount=70.0, status="promised")
        cheque = _due(test_db_session, c, num="ASS/1", amount=80.0, payment_pending="assegno")
        near_noprom = _inv(test_db_session, c, "NEAR/NOPROM", 10.0, DUE6)
        near_promised = _due(test_db_session, c, num="NEAR/PROM", amount=20.0, due=DUE6, status="promised")
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["merged_early"]) == (1, 1, 0)
        # In finestra e in perimetro: OK, PROM, ASS. Le «prossime» escluse non contano né si registrano.
        assert (summary["candidates"], summary["skipped"], summary["out_of_scope"]) == (3, 2, 1)
        sent = mailer.sent[0]
        assert sent["subject"] == "Sollecito di pagamento: fattura n. OK/1 scaduta da 7 giorni (100,00 €)"
        # Le altre scadute CERTE del cliente sono citate (anche senza promemoria); promesse e assegni no.
        assert ("Risultano inoltre scadute e non saldate altre 2 fatture per un totale di 60,00 €. "
                "Totale da saldare: 160,00 €.") in sent["text"]
        assert [n for n, _ in sent["attachments"]] == [
            "copia_cortesia_fattura_OK_1.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        rows = {r.invoice_id: (r.status, r.skip_reason, r.group_key is not None) for r in _rows(test_db_session)}
        assert rows == {ok.id: ("sent", None, True), promised.id: ("skipped", "promessa", False),
                        cheque.id: ("skipped", "in_incasso", False)}
        assert {noprom.id, near_noprom.id, near_promised.id} & set(rows) == set()

    def test_customer_exclusion_skips_window_invoices_and_writes_nothing_for_near_ones(self, test_db_session):
        c = _cust(test_db_session, recovery_status="waiting")
        a = _due(test_db_session, c, num="A/1", amount=10.0)
        b = _due(test_db_session, c, num="B/2", amount=20.0)
        near = _due(test_db_session, c, num="C/3", amount=30.0, due=DUE6)
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["skipped"], summary["candidates"]) == (0, 2, 2) and mailer.calls == 0
        assert {r.invoice_id: (r.status, r.skip_reason) for r in _rows(test_db_session)} == {
            a.id: ("skipped", "in_attesa"), b.id: ("skipped", "in_attesa")}
        # L'operatore chiude l'attesa: il giorno dopo UNA email con tutte e tre.
        c.recovery_status = "idle"
        test_db_session.commit()
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        second = run_followups(test_db_session, now_utc=next_day, mailer=mailer, cfg=_on_cfg())
        assert (second["sent"], second["invoices"], second["skipped"], second["merged_early"]) == (1, 3, 0, 0)
        assert mailer.sent[0]["subject"] == "Sollecito di pagamento: 3 fatture scadute (60,00 €)"
        rows = _rows(test_db_session)
        assert {r.invoice_id for r in rows} == {a.id, b.id, near.id} and {r.status for r in rows} == {"sent"}
        assert len({r.group_key for r in rows}) == 1 and len(rows) == 3  # stesse righe, aggiornate

    def test_invoice_paid_meanwhile_leaves_the_email(self, test_db_session):
        alfa = _other(test_db_session, 1, nome="Alfa Srl")
        beta = _other(test_db_session, 2, nome="Beta Srl")
        a1 = _due(test_db_session, alfa, num="A/1")
        b1 = _due(test_db_session, beta, num="B/1", amount=100.0)
        b2 = _due(test_db_session, beta, num="B/2", amount=200.0)
        _sync(test_db_session)

        class PaysOne(FakeMailer):
            """Mentre parte l'email di Alfa, il sync segna pagata una fattura di Beta."""
            def send(self, **kwargs):
                out = super().send(**kwargs)
                if self.calls == 1:
                    test_db_session.query(Invoice).filter(Invoice.id == b2.id).update(
                        {"status": "paid", "amount_due": 0.0})
                return out

        mailer = PaysOne()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["settled_meanwhile"], summary["candidates"]) == (2, 2, 1, 2)
        assert mailer.sent[1]["subject"] == "Sollecito di pagamento: fattura n. B/1 scaduta da 7 giorni (100,00 €)"
        assert "B/2" not in mailer.sent[1]["text"] and "Risult" not in mailer.sent[1]["text"]
        assert {r.invoice_id for r in _rows(test_db_session)} == {a1.id, b1.id}

    def test_near_invoice_paid_meanwhile_is_simply_not_merged(self, test_db_session):
        alfa = _other(test_db_session, 1, nome="Alfa Srl")
        beta = _other(test_db_session, 2, nome="Beta Srl")
        _due(test_db_session, alfa, num="A/1")
        b1 = _due(test_db_session, beta, num="B/1", amount=100.0)
        near = _due(test_db_session, beta, num="B/2", amount=200.0, due=DUE6)
        _sync(test_db_session)

        class PaysTheNearOne(FakeMailer):
            def send(self, **kwargs):
                out = super().send(**kwargs)
                if self.calls == 1:
                    test_db_session.query(Invoice).filter(Invoice.id == near.id).update(
                        {"status": "paid", "amount_due": 0.0})
                return out

        mailer = PaysTheNearOne()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        # Non era ancora nel giro: non conta tra le «saldate nel frattempo».
        assert (summary["sent"], summary["invoices"], summary["settled_meanwhile"], summary["merged_early"]) == (2, 2, 0, 0)
        assert "fattura n. B/1" in mailer.sent[1]["subject"] and b1.id in {r.invoice_id for r in _rows(test_db_session)}
        assert near.id not in {r.invoice_id for r in _rows(test_db_session)}

    # ── Una sola volta: riserva, errori, ritenti ──

    def _pair(self, s, **cust_kw):
        c = _cust(s, **cust_kw)
        a = _due(s, c, num="A/1", amount=100.0)
        b = _due(s, c, num="B/2", amount=200.0, due=DUE6)
        _sync(s)
        return c, a, b

    def test_uncertain_failure_stops_the_whole_email_and_retry_sends_one_email(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        first = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (first["failed"], first["sent"], first["invoices"]) == (1, 0, 0)
        rows = _rows(test_db_session)
        assert [(r.status, r.error_kind) for r in rows] == [("failed", "network")] * 2
        assert len({r.group_key for r in rows}) == 1 and rows[0].group_key
        assert sorted(i["group_size"] for i in first["items"]) == [2, 2]
        # Stato incerto: il giro non ritenta da solo, per nessuna delle due
        # (oggi conta solo A: B, scadenza prossima, non è ancora in finestra)…
        again = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (again["sent"], again["failed"], again["already"]) == (0, 0, 1) and mailer.calls == 1
        # …né il giorno dopo, quando anche B è in finestra.
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        tomorrow = run_followups(test_db_session, now_utc=next_day, mailer=mailer, cfg=_on_cfg())
        assert (tomorrow["sent"], tomorrow["failed"], tomorrow["already"]) == (0, 0, 2) and mailer.calls == 1
        _sync(test_db_session)
        # «Riprova» su UNA riga rimanda l'email intera, una volta.
        row_b = [r for r in rows if r.invoice_id == b.id][0]
        retried = retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg())
        assert (retried["sent"], retried["invoices"], retried["candidates"]) == (1, 2, 2)
        assert mailer.calls == 2 and len(mailer.sent) == 1
        assert mailer.sent[0]["subject"] == "Sollecito di pagamento: 2 fatture scadute (300,00 €)"
        # Il ritento usa la chiave del tentativo fallito (nessun doppione se quello era partito).
        assert mailer.sent[0]["idempotency_key"] == mailer.keys[0] and mailer.keys[0].startswith(_idem(c, [a, b]))
        rows = _rows(test_db_session)
        assert [(r.status, r.error, r.provider_message_id) for r in rows] == [("sent", None, "msg-2")] * 2
        assert len(rows) == 2
        with pytest.raises(ValueError, match="falliti o interrotti"):
            retry_followup_row(test_db_session, rows[0], mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 2

    def test_retry_leaves_out_invoices_no_longer_due(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        b.status = "paid"
        b.amount_due = 0.0
        test_db_session.commit()
        row_a, row_b = _rows(test_db_session)
        with pytest.raises(ValueError, match="non più da sollecitare"):
            retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg())
        retried = retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg())
        assert (retried["sent"], retried["invoices"], retried["candidates"]) == (1, 1, 1)
        assert mailer.sent[0]["subject"] == "Sollecito di pagamento: fattura n. A/1 scaduta da 7 giorni (100,00 €)"
        assert "B/2" not in mailer.sent[0]["text"]
        # Le fatture sono cambiate, ma la chiave verso Resend è quella del primo
        # tentativo: se quello era arrivato, per 24 ore Resend ferma anche questo.
        assert mailer.sent[0]["idempotency_key"] == mailer.keys[0] and mailer.keys[0].startswith(_idem(c, [a, b]))
        assert row_a.group_key == group_key(c.id, [a], TODAY) != row_b.group_key  # nel registro: un'altra email
        assert (row_a.status, row_b.status) == ("sent", "failed")  # la riga della pagata non cambia

    def test_retry_skips_members_with_a_changed_due_date(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row_a, row_b = _rows(test_db_session)
        b.due_date = date(2026, 11, 30)  # scadenza spostata dopo il tentativo
        test_db_session.commit()
        retried = retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg())
        assert (retried["sent"], retried["invoices"]) == (1, 1) and "B/2" not in mailer.sent[0]["text"]
        assert (row_a.status, row_b.status) == ("sent", "failed")

    def test_retry_only_takes_stuck_rows_of_the_same_email(self, test_db_session):
        # Righe costruite a mano, come le lascerebbe un dato sporco: il ritento
        # prende SOLO le righe ferme della stessa email e dello stesso cliente.
        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="A/1", amount=100.0)
        sent_twin = _due(test_db_session, c, num="B/2", amount=200.0)          # stessa chiave, ma già inviata
        no_key = _due(test_db_session, c, num="C/3", amount=300.0)             # ferma, senza chiave (riga orfana)
        stranger = _other(test_db_session, 2)
        foreign = _due(test_db_session, stranger, num="X/1", amount=400.0)     # stessa chiave, altro cliente
        _sync(test_db_session)

        def row(inv, status, key, **kw):
            r = InvoiceFollowupEmail(invoice_id=inv.id, customer_id=inv.customer_id, due_date=inv.due_date,
                                     status=status, mode="reale", group_key=key, **kw)
            test_db_session.add(r)
            test_db_session.commit()
            return r

        stuck = row(a, "failed", "chiave-1", error_kind="network")
        twin = row(sent_twin, "sent", "chiave-1")
        orphan = row(no_key, "sending", None)
        alien = row(foreign, "failed", "chiave-1", error_kind="network")
        # stessa chiave, stesso cliente, promemoria inviato, ma NON ancora scaduta (scade oggi)
        not_due = row(_due(test_db_session, c, num="OGGI/1", amount=70.0, due=TODAY), "failed", "chiave-1",
                      error_kind="network")
        mailer = FakeMailer()
        out = retry_followup_row(test_db_session, stuck, mailer=mailer, cfg=_on_cfg())
        assert (out["sent"], out["invoices"], out["candidates"]) == (1, 1, 1)
        assert mailer.sent[0]["subject"].startswith("Sollecito di pagamento: fattura n. A/1 ")
        assert mailer.sent[0]["to"] == ["amministrazione@shabu.it"] and "OGGI/1" not in mailer.sent[0]["text"]
        assert not_due.status == "failed"
        assert (stuck.status, twin.status, orphan.status, alien.status) == ("sent", "sent", "sending", "failed")
        assert twin.provider_message_id is None and alien.group_key == "chiave-1"
        # Email mista (dato sporco): una riga col rifiuto certo, una dall'esito
        # incerto, una già fermata da un 409. Ritentando la «certa» l'email
        # riparte con la fattura incerta — quindi con la SUA chiave verso
        # Resend, e da tentativo che resta incerto — e senza quella in 409.
        p1 = _due(test_db_session, c, num="P/1", amount=11.0)
        p2 = _due(test_db_session, c, num="P/2", amount=12.0)
        p3 = _due(test_db_session, c, num="P/3", amount=13.0)
        certain = row(p1, "failed", "chiave-2", error_kind="validation", idempotency_key="k-certa",
                      key_first_used_at=datetime(2026, 11, 18, 9, 0))
        unsure = row(p2, "failed", "chiave-2", error_kind="network", idempotency_key="k-incerta",
                     key_first_used_at=datetime(2026, 11, 18, 7, 0))
        blocked = row(p3, "failed", "chiave-2", error_kind="conflict", idempotency_key="k-incerta")
        refusing = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        out = retry_followup_row(test_db_session, certain, mailer=refusing, cfg=_on_cfg())
        assert (out["failed"], out["candidates"]) == (1, 2) and refusing.keys == ["k-incerta"]
        # La chiave riusata è nata alle 7: vale per tutte le righe del tentativo (il primo uso più vecchio).
        assert certain.key_first_used_at == unsure.key_first_used_at == datetime(2026, 11, 18, 7, 0)
        assert (certain.error_kind, unsure.error_kind, blocked.error_kind) == ("other", "other", "conflict")
        assert certain.group_key == unsure.group_key != blocked.group_key
        out = retry_followup_row(test_db_session, unsure, mailer=refusing, cfg=_on_cfg())
        assert (out["sent"], out["invoices"]) == (1, 2) and refusing.keys == ["k-incerta", "k-incerta"]
        assert "P/3" not in refusing.sent[0]["text"] and blocked.status == "failed"
        # Una riga senza chiave si ritenta da sola: non trascina le altre senza chiave.
        other_orphan = row(_due(test_db_session, c, num="D/4", amount=50.0), "sending", None)
        out = retry_followup_row(test_db_session, orphan, mailer=mailer, cfg=_on_cfg())
        assert (out["sent"], out["invoices"]) == (1, 1) and "fattura n. C/3" in mailer.sent[1]["subject"]
        assert (orphan.status, other_orphan.status) == ("sent", "sending")

    # ── Correzioni dopo la revisione avversariale (24/11/2026) ──

    def test_retry_never_mixes_customers_after_a_rematch(self, test_db_session):
        # Tra il tentativo fallito e «Riprova» una fattura passa a un altro
        # cliente (riabbinamento): l'email va a chi possiede ORA la fattura
        # scelta, con le sole fatture ancora sue.
        s = test_db_session
        ics = _other(s, 11, nome="Ics Srl")
        zeta = _other(s, 22, nome="Zeta Srl")
        a = _due(s, ics, num="A/1", amount=100.0)
        b = _due(s, ics, num="B/2", amount=200.0, due=DUE6)
        _sync(s)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(s, mailer=mailer, cfg=_on_cfg())
        row_a, row_b = _rows(s)
        b.customer_id, b.customer_name_raw, b.customer_piva_raw = zeta.id, zeta.ragione_sociale, zeta.partita_iva
        s.commit()
        out = retry_followup_row(s, row_a, mailer=mailer, cfg=_on_cfg())
        assert (out["sent"], out["invoices"], out["candidates"]) == (1, 1, 1)
        sent = mailer.sent[0]
        assert sent["to"] == ["amministrazione@cliente11.it"] and "Spett.le Ics Srl" in sent["text"]
        assert "B/2" not in sent["text"]
        assert [n for n, _ in sent["attachments"]] == ["copia_cortesia_fattura_A_1.pdf"]
        assert (row_a.status, row_b.status, row_b.customer_id) == ("sent", "failed", ics.id)
        # La fattura passata a Zeta: Zeta non ha mai ricevuto il promemoria,
        # quindi nessun secondo avviso (né oggi né dal giro), e la riga non cambia.
        assert reminder_sent(s, b) is None
        with pytest.raises(ValueError, match="Promemoria pre-scadenza non inviato"):
            retry_followup_row(s, row_b, mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 2 and row_b.status == "failed"
        next_day = _at(date(2026, 11, 19))
        _sync(s, now=next_day)
        later = run_followups(s, now_utc=next_day, mailer=mailer, cfg=_on_cfg())
        assert (later["sent"], later["out_of_scope"]) == (0, 1) and mailer.calls == 2

    def test_group_engine_refuses_invoices_of_another_customer(self, test_db_session):
        # Cintura: qualunque strada porti al motore, un'email non mescola clienti.
        s = test_db_session
        ics, zeta = _other(s, 11), _other(s, 22)
        a = _due(s, ics, num="A/1")
        x = _due(s, zeta, num="X/1")
        mailer = FakeMailer()
        summary = fu._summary(MODE_REALE, [], None)
        fu._process_group_safely(s, ics, [a, x], key="k", mode=MODE_REALE, mailer=mailer, cfg=_on_cfg(),
                                 summary=summary, existing={}, today=TODAY)
        assert mailer.calls == 0 and summary["sent"] == 0 and summary["failed"] == 1
        assert {(r.status, r.error_kind) for r in _rows(s)} == {("failed", "not_attempted")}
        assert all("altro cliente" in r.error for r in _rows(s))

    def test_retry_refuses_a_lone_invoice_not_yet_at_seven_days(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        a.status, a.amount_due = "paid", 0.0   # quella a 7 giorni viene pagata
        test_db_session.commit()
        row_b = [r for r in _rows(test_db_session) if r.invoice_id == b.id][0]
        with pytest.raises(ValueError, match="meno di 7 giorni"):
            retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 1 and row_b.status == "failed"
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        out = retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg(), now_utc=next_day)
        assert out["sent"] == 1 and "è scaduta da 7 giorni (scadenza 12/11/2026)" in mailer.sent[0]["text"]

    def test_retry_while_excluded_changes_nothing(self, test_db_session):
        # Un ritento non trasforma mai una riga dall'esito incerto in
        # «saltata» (che il giro poi rimanderebbe da solo): resta ferma.
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row_a, row_b = _rows(test_db_session)
        b.status = "promised"  # nel frattempo l'operatore ha segnato la promessa su B
        test_db_session.commit()
        with pytest.raises(ValueError, match="non inviabile ora: Pagamento promesso"):
            retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg())
        assert (row_b.status, row_b.error_kind, row_b.skip_reason) == ("failed", "network", None)
        # A parte da sola; la riga di B resta com'è (né inviata né «saltata»).
        out = retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg())
        assert (out["sent"], out["invoices"], out["skipped"]) == (1, 1, 0)
        assert "B/2" not in mailer.sent[0]["text"] and "fattura n. A/1" in mailer.sent[0]["subject"]
        assert (row_a.status, row_b.status, row_b.group_key is not None) == ("sent", "failed", True)
        # Tutto il cliente in attesa: nessun invio, righe intatte…
        c2, a2, b2 = self._pair(test_db_session, nome="Altro Srl", email="a@altro.it", partita_iva="00000000008")
        timeout = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=timeout, cfg=_on_cfg())
        c2.recovery_status = "waiting"
        test_db_session.commit()
        stuck = test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=a2.id).one()
        with pytest.raises(ValueError, match="non inviabile ora: Pratica in attesa"):
            retry_followup_row(test_db_session, stuck, mailer=timeout, cfg=_on_cfg())
        assert timeout.calls == 1 and (stuck.status, stuck.error_kind) == ("failed", "network")
        # …e quando l'attesa finisce il giro NON le rimanda da solo: decide un operatore.
        c2.recovery_status = "idle"
        test_db_session.commit()
        for day in (TODAY, date(2026, 11, 19), date(2026, 11, 20)):
            _sync(test_db_session, now=_at(day))
            assert run_followups(test_db_session, now_utc=_at(day), mailer=timeout, cfg=_on_cfg())["sent"] == 0
        assert timeout.calls == 1

    def test_second_click_finds_the_row_already_sent(self, test_db_session):
        from sqlalchemy.orm.attributes import set_committed_value
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row_a = _rows(test_db_session)[0]
        assert retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        # Una seconda richiesta che ha in mano la riga com'era PRIMA del primo ritento.
        set_committed_value(row_a, "status", "failed")
        with pytest.raises(ValueError, match="falliti o interrotti"):
            retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 2 and len(mailer.sent) == 1 and row_a.status == "sent"
        assert not re_mod._reminder_lock.locked()

    def test_existing_row_taken_by_another_round_is_not_sent(self, test_db_session, monkeypatch):
        # Una riga GIÀ esistente (qui: saltata, da rivalutare) viene presa da un
        # altro processo tra la lettura e la riserva: l'email non parte.
        from sqlalchemy.orm import Session as SASession
        c = _cust(test_db_session, recovery_status="waiting")
        inv = _due(test_db_session, c)
        _sync(test_db_session)
        run_followups(test_db_session, mailer=FakeMailer(), cfg=_on_cfg())
        row = test_db_session.query(InvoiceFollowupEmail).one()
        assert row.status == "skipped"
        c.recovery_status = "idle"
        test_db_session.commit()
        real = fu.compose_followup

        def other_round_takes_it(*args, **kwargs):
            other = SASession(bind=test_db_session.get_bind())
            other.query(InvoiceFollowupEmail).filter_by(id=row.id).update({"status": "sending"})
            other.commit()
            other.close()
            return real(*args, **kwargs)

        monkeypatch.setattr(fu, "compose_followup", other_round_takes_it)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["failed"], summary["already"]) == (0, 0, 1) and mailer.calls == 0
        test_db_session.expire_all()
        assert test_db_session.query(InvoiceFollowupEmail).one().status == "sending"  # com'era stata lasciata
        assert inv.status == "open"

    def test_conflict_from_resend_needs_an_explicit_confirmation(self, test_db_session):
        c, a, b = self._pair(test_db_session)

        class Conflict(FakeMailer):
            def send(self, **kwargs):
                self.calls += 1
                raise MailSendError("Resend HTTP 409: This idempotency key has already been used", kind="validation")

        mailer = Conflict()
        first = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (first["failed"], first["sent"]) == (1, 0)
        rows = _rows(test_db_session)
        # 409 = «un invio con questa chiave mi è già arrivato»: NON è un rifiuto certo.
        assert [(r.status, r.error_kind) for r in rows] == [("failed", "conflict")] * 2
        assert all(r.error.startswith("Resend ha già ricevuto un invio con la stessa chiave") for r in rows)
        assert "Resend HTTP 409" in rows[0].error and "«Invia comunque»" in rows[0].error
        for day in (TODAY, date(2026, 11, 19), date(2026, 11, 20)):
            _sync(test_db_session, now=_at(day))
            again = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
            assert (again["sent"], again["failed"]) == (0, 0), day
        assert mailer.calls == 1  # mai ritentata da sola…
        assert fu._needs_processing(rows[0], MODE_REALE) == (False, "Resend l'aveva già ricevuta: decide un operatore")
        _sync(test_db_session)
        plain = FakeMailer()
        for row in rows:        # …e a mano solo con la conferma «ho controllato su Resend, non è arrivata»
            assert fu.needs_confirmation(row) is True
            with pytest.raises(ValueError, match="solo se NON risulta, confermare «Invia comunque»"):
                retry_followup_row(test_db_session, row, mailer=plain, cfg=_on_cfg())
        assert plain.calls == 0
        assert [(r.status, r.error_kind) for r in _rows(test_db_session)] == [("failed", "conflict")] * 2
        old_key = rows[0].idempotency_key
        out = retry_followup_row(test_db_session, rows[1], mailer=plain, cfg=_on_cfg(), force=True)
        assert (out["sent"], out["invoices"]) == (1, 2) and "2 fatture scadute" in plain.sent[0]["subject"]
        assert plain.sent[0]["idempotency_key"] != old_key  # tentativo nuovo: chiave nuova
        assert {r.status for r in _rows(test_db_session)} == {"sent"}
        # Un vero rifiuto (422) resta «certamente non partita» e si ritenta da solo.
        assert fu._is_conflict(MailSendError("Resend HTTP 422: invalid recipient", kind="validation")) is False
        assert fu._is_conflict(MailSendError("Resend non raggiungibile: ReadTimeout", kind="network")) is False

    def test_a_forced_attempt_that_fails_is_still_never_retried_alone(self, test_db_session):
        # «Invia comunque» dopo un 409, e l'invio fallisce con un rifiuto: la
        # riga resta ferma (il tentativo di prima potrebbe essere arrivato).
        c = _cust(test_db_session)
        _due(test_db_session, c, num="A/1")
        _sync(test_db_session)
        mailer = self._Resend()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row = _rows(test_db_session)[0]
        retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())           # 409 → conflitto
        refused = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        out = retry_followup_row(test_db_session, row, mailer=refused, cfg=_on_cfg(), force=True)
        assert out["failed"] == 1 and (row.status, row.error_kind) == ("failed", "other")
        assert run_followups(test_db_session, mailer=refused, cfg=_on_cfg())["sent"] == 0 and refused.calls == 1

    def test_an_old_uncertain_real_send_needs_the_confirmation_too(self, test_db_session):
        # Oltre un giorno dal PRIMO uso della chiave Resend l'ha dimenticata:
        # un ritento non sarebbe più protetto. Serve la conferma esplicita.
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row_a, row_b = _rows(test_db_session)
        assert row_a.key_first_used_at == row_a.attempted_at == row_b.key_first_used_at
        now = datetime(2026, 11, 18, 10, 0)
        for hours, expected in ((1, False), (22, False), (24, True), (72, True)):
            row_a.key_first_used_at = now - timedelta(hours=hours)
            assert fu.needs_confirmation(row_a, now) is expected, hours
        row_a.key_first_used_at = row_b.key_first_used_at = now - timedelta(hours=30)
        test_db_session.commit()
        with pytest.raises(ValueError, match="Resend non fermerebbe più un doppione"):
            retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg())
        assert mailer.calls == 1 and row_a.status == "failed"
        first_key = row_a.idempotency_key
        out = retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg(), force=True)
        assert (out["sent"], out["invoices"]) == (1, 2) and mailer.sent[0]["idempotency_key"] != first_key
        assert row_a.key_first_used_at == row_a.attempted_at  # tentativo nuovo, chiave nuova: l'orologio riparte
        # Mai per le righe di prova/test (non riguardano il cliente) né per un rifiuto certo.
        row_a.status, row_a.error_kind, row_a.mode = "failed", "network", "test"
        assert fu.needs_confirmation(row_a, now + timedelta(days=9)) is False
        row_a.mode, row_a.error_kind = "reale", "validation"
        assert fu.needs_confirmation(row_a, now + timedelta(days=9)) is False
        # Righe nate prima della colonna: vale l'ultimo tentativo; senza nemmeno quello, la nascita della riga.
        row_a.error_kind, row_a.key_first_used_at, row_a.attempted_at = "network", None, now - timedelta(hours=30)
        assert fu.needs_confirmation(row_a, now) is True
        row_a.attempted_at, row_a.created_at = None, now - timedelta(hours=2)
        assert fu.needs_confirmation(row_a, now) is False
        row_a.created_at = now - timedelta(hours=40)
        assert fu.needs_confirmation(row_a, now) is True
        row_a.created_at = None
        assert fu.needs_confirmation(row_a, now) is True   # età ignota: non si conta su Resend
        # Vista da un'altra modalità la riga non chiede conferma (lì è un invio nuovo).
        row_a.created_at, row_a.error_kind = now - timedelta(hours=40), "conflict"
        assert fu.needs_confirmation(row_a, now, MODE_REALE) is True
        assert fu.needs_confirmation(row_a, now, MODE_TEST) is False

    def test_a_failed_retry_does_not_restart_the_24h_clock(self, test_db_session):
        # T0: email consegnata ma risposta persa (chiave K). T0+7h: «Riprova»
        # con K non arriva a Resend. T0+25h: Resend ha dimenticato K → un
        # terzo «Riprova» deve chiedere la conferma, anche se dall'ULTIMO
        # tentativo sono passate solo 18 ore.
        c = _cust(test_db_session)
        _due(test_db_session, c, num="A/1")
        _sync(test_db_session)
        mailer = FakeMailer(fail_times=2, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row = _rows(test_db_session)[0]
        t0 = row.attempted_at
        retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())       # stesso tentativo, stessa chiave
        assert mailer.keys[0] == mailer.keys[1] and row.error_kind == "network"
        assert row.key_first_used_at == t0 and row.attempted_at > t0                 # l'orologio della chiave NON riparte
        assert fu.needs_confirmation(row, t0 + timedelta(hours=22)) is False
        assert fu.needs_confirmation(row, t0 + timedelta(hours=25)) is True
        # Con due fatture: anche quella che entra nel ritento prende la nascita della chiave, non «adesso».
        c2, a2, b2 = self._pair(test_db_session, nome="Altro Srl", email="a@altro.it", partita_iva="00000000008")
        pair = FakeMailer(fail_times=2, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=pair, cfg=_on_cfg())
        rows = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c2.id).all()
        born = rows[0].key_first_used_at
        retry_followup_row(test_db_session, rows[1], mailer=pair, cfg=_on_cfg())
        assert {r.key_first_used_at for r in rows} == {born} and all(r.attempted_at > born for r in rows)

    def test_a_forced_retry_never_releases_the_other_invoices(self, test_db_session):
        # Chiave nata da più di un giorno: il ritento chiede la conferma e
        # parte con una chiave NUOVA. Che venga accettata non dimostra nulla
        # sul primo tentativo: la fattura rimasta fuori resta incerta, con la
        # sua vecchia chiave (e chiederà la conferma a sua volta).
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row_a, row_b = _rows(test_db_session)
        old = datetime(2026, 11, 16, 9, 0)
        row_a.key_first_used_at = row_b.key_first_used_at = old
        b.status = "promised"
        test_db_session.commit()
        key = row_b.idempotency_key
        assert retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg(), force=True)["sent"] == 1
        assert (row_b.status, row_b.idempotency_key, row_b.key_first_used_at) == ("failed", key, old)
        b.status = "open"
        test_db_session.commit()
        with pytest.raises(ValueError, match="Invia comunque"):
            retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg())

    def test_manual_retry_never_sends_in_the_weekend(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row = _rows(test_db_session)[0]
        for day in (date(2026, 11, 21), date(2026, 11, 22)):
            _sync(test_db_session, now=_at(day))
            with pytest.raises(ValueError, match="nel weekend non si inviano avvisi"):
                retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg(), now_utc=_at(day))
        assert mailer.calls == 1

    def test_real_connector_409_is_recognised_as_conflict(self):
        # Il riconoscimento del 409 dipende dal testo che produce il connettore:
        # questo test lega le due cose.
        import httpx
        from backend.connectors.resend_mail import ResendMailer

        def answer(status, name):
            def handler(request):
                return httpx.Response(status, json={"name": name, "message": name})
            return handler

        for status, name, conflict in ((409, "invalid_idempotent_request", True),
                                       (409, "concurrent_idempotent_requests", True),
                                       (422, "validation_error", False), (429, "rate_limit_exceeded", False)):
            mailer = ResendMailer("re_key", "https://api.resend.test", transport=httpx.MockTransport(answer(status, name)))
            with pytest.raises(MailSendError) as exc:
                mailer.send(sender="x@sakecompany.com", to=["a@b.it"], subject="S", text="t")
            assert fu._is_conflict(exc.value) is conflict, (status, name)

    class _Resend(FakeMailer):
        """Resend com'è documentato: ricorda le chiavi (24 ore) e a una chiave
        già vista risponde 409. `lost` = quante richieste ACCETTA ma la cui
        risposta si perde (timeout): il caso per cui esiste la chiave."""

        def __init__(self, lost=1):
            super().__init__()
            self.delivered, self.seen, self.lost = [], set(), lost

        def send(self, **kwargs):
            self.calls += 1
            self.keys.append(kwargs["idempotency_key"])
            if kwargs["idempotency_key"] in self.seen:
                raise MailSendError("Resend HTTP 409: invalid_idempotent_request", kind="validation")
            self.seen.add(kwargs["idempotency_key"])
            self.delivered.append(kwargs)
            if self.lost > 0:
                self.lost -= 1
                raise MailSendError("Resend non raggiungibile: ReadTimeout", kind="network")
            return "msg"

    def test_timeout_then_retry_then_next_round_never_duplicates(self, test_db_session):
        # Timeout, ma Resend aveva accettato l'email. Il ritento riceve 409
        # (stessa chiave, contenuto diverso): da lì in poi nessuno la rimanda.
        c = _cust(test_db_session)
        _due(test_db_session, c, num="A/1", amount=100.0)
        _sync(test_db_session)
        mailer = self._Resend()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())               # timeout, ma consegnata
        row = _rows(test_db_session)[0]
        assert (row.status, row.error_kind) == ("failed", "network")
        out = retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())  # stesso giorno → 409
        assert (out["sent"], out["failed"]) == (0, 1) and (row.status, row.error_kind) == ("failed", "conflict")
        for day in (date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)):
            _sync(test_db_session, now=_at(day))
            run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
        assert len(mailer.delivered) == 1 and mailer.calls == 2 and len(set(mailer.keys)) == 1
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        with pytest.raises(ValueError, match="«Invia comunque»"):
            retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg(), now_utc=next_day)
        assert len(mailer.delivered) == 1

    def test_retry_the_next_day_and_with_other_invoices_still_uses_the_first_key(self, test_db_session):
        # Il doppione più naturale: {A, B} va in timeout ma è arrivata, il
        # cliente paga A, l'operatore ritenta B il giorno dopo. La chiave
        # verso Resend è quella del primo tentativo → 409 → nessun doppione.
        c, a, b = self._pair(test_db_session)
        mailer = self._Resend()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row_a, row_b = _rows(test_db_session)
        a.status, a.amount_due = "paid", 0.0
        test_db_session.commit()
        next_day = _at(date(2026, 11, 19))
        _sync(test_db_session, now=next_day)
        out = retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg(), now_utc=next_day)
        assert (out["sent"], out["failed"]) == (0, 1) and len(mailer.delivered) == 1
        assert mailer.keys[1] == mailer.keys[0]
        assert (row_b.status, row_b.error_kind) == ("failed", "conflict") and row_a.status == "failed"
        # Se invece il primo tentativo NON era arrivato a Resend, lo stesso ritento parte normalmente.
        c2, a2, b2 = self._pair(test_db_session, nome="Altro Srl", email="a@altro.it", partita_iva="00000000008")
        plain = FakeMailer(fail_times=2, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=plain, cfg=_on_cfg())
        stuck = test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=b2.id).one()
        _sync(test_db_session, now=next_day)
        retry_followup_row(test_db_session, stuck, mailer=plain, cfg=_on_cfg(), now_utc=next_day)   # fallisce ancora
        assert (stuck.status, stuck.error_kind) == ("failed", "network")
        out = retry_followup_row(test_db_session, stuck, mailer=plain, cfg=_on_cfg(), now_utc=next_day)
        assert out["sent"] == 1 and plain.keys[0] == plain.keys[1] == plain.keys[2] == plain.sent[0]["idempotency_key"]
        assert "scaduta da 8 giorni" in plain.sent[0]["text"]  # il testo è quello di oggi
        rows = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c2.id).all()
        assert {r.status for r in rows} == {"sent"} and {r.idempotency_key for r in rows} == {plain.keys[0]}
        assert {r.group_key for r in rows} == {group_key(c2.id, [a2, b2], date(2026, 11, 19))}

    def test_a_certain_refusal_gets_a_new_key_at_every_attempt(self, test_db_session):
        # Un rifiuto certo (nulla è partito) ritentato non deve mai prendere un
        # 409 «probabilmente partita»: ogni tentativo nuovo ha la sua chiave.
        c, a, b = self._pair(test_db_session)
        refused = FakeMailer(fail_times=2, kind=ERROR_VALIDATION)
        for _ in range(3):   # tre giri lo stesso giorno: due rifiuti, poi parte
            run_followups(test_db_session, mailer=refused, cfg=_on_cfg())
        assert len(refused.sent) == 1 and len(set(refused.keys)) == 3
        assert all(k.startswith(_idem(c, [a, b])) for k in refused.keys)
        # Anche a mano: «Riprova» su un rifiuto certo usa una chiave nuova.
        c2, a2, b2 = self._pair(test_db_session, nome="Altro Srl", email="a@altro.it", partita_iva="00000000008")
        again = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        run_followups(test_db_session, mailer=again, cfg=_on_cfg())
        stuck = test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=a2.id).one()
        assert retry_followup_row(test_db_session, stuck, mailer=again, cfg=_on_cfg())["sent"] == 1
        assert again.keys[0] != again.keys[1]

    def test_an_uncertain_outcome_stays_uncertain_until_the_email_really_goes(self, test_db_session, monkeypatch):
        # Dopo un timeout (forse arrivata) un ritento che fallisce in modo
        # «certo» NON deve rendere la riga ritentabile dal giro: il giro la
        # rimanderebbe da solo, con una chiave nuova.
        next_day = _at(date(2026, 11, 19))

        def stuck_row(n):
            c = _other(test_db_session, n)
            _due(test_db_session, c, num=f"U/{n}", amount=100.0)
            _sync(test_db_session)
            mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
            run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
            return mailer, test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c.id).one()

        def rounds_send_nothing(mailer):
            for day in (TODAY, date(2026, 11, 19), date(2026, 11, 20)):
                _sync(test_db_session, now=_at(day))
                out = run_followups(test_db_session, now_utc=_at(day), mailer=mailer, cfg=_on_cfg())
                assert (out["sent"], out["prepared"]) == (0, 0), day
            _sync(test_db_session)

        # 1) il ritento inciampa nel PDF
        mailer, row = stuck_row(1)
        real = fu.build_followup_attachments
        monkeypatch.setattr(fu, "build_followup_attachments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("font")))
        retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())
        monkeypatch.setattr(fu, "build_followup_attachments", real)
        assert (row.status, row.error_kind) == ("failed", "other") and "PDF non generato" in row.error
        rounds_send_nothing(mailer)
        assert mailer.calls == 1
        # 2) il ritento riceve un rifiuto (es. 429)
        mailer, row = stuck_row(2)
        mailer.fail_times, mailer.kind = 1, ERROR_VALIDATION
        retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())
        assert (row.status, row.error_kind) == ("failed", "other")
        rounds_send_nothing(mailer)
        assert mailer.calls == 2 and mailer.keys[0] == mailer.keys[1]
        # …ma un operatore può ancora ritentarla, sempre con la stessa chiave.
        assert retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert mailer.keys[2] == mailer.keys[0]
        # 3) e 4) il ritento in prova o in test di un invio REALE incerto è rifiutato: la riga non cambia
        for n, cfg in ((3, _cfg(REMINDER_FOLLOWUP_ENABLED=True)), (4, _on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com"))):
            mailer, row = stuck_row(n)
            with pytest.raises(ValueError, match="solo in modalità reale"):
                retry_followup_row(test_db_session, row, mailer=mailer, cfg=cfg)
            assert (row.status, row.mode, row.error_kind) == ("failed", "reale", "network")
            rounds_send_nothing(mailer)
            assert mailer.calls == 1
        # Un tentativo incerto fatto in TEST (il cliente non c'entrava) si può invece ritentare in reale.
        c5 = _other(test_db_session, 5)
        _due(test_db_session, c5, num="U/5")
        test_cfg = _on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=test_cfg)
        row = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c5.id).one()
        assert (row.mode, row.error_kind) == ("test", "network")
        assert retry_followup_row(test_db_session, row, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert mailer.sent[0]["to"] == ["amministrazione@cliente5.it"]
        # …e anche di nuovo in test (va all'indirizzo di prova): poi il giro reale la manda al cliente.
        c6 = _other(test_db_session, 6)
        _due(test_db_session, c6, num="U/6")
        mailer = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=mailer, cfg=test_cfg)
        row = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c6.id).one()
        assert retry_followup_row(test_db_session, row, mailer=mailer, cfg=test_cfg)["sent"] == 1
        assert mailer.sent[0]["to"] == ["lorenzo@example.com"] and (row.status, row.mode) == ("sent", "test")
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert mailer.sent[1]["to"] == ["amministrazione@cliente6.it"]

    def test_reminder_to_a_merged_customer_card_still_counts(self, test_db_session):
        # La scheda che ha ricevuto il promemoria viene UNITA a un'altra
        # (stessa azienda, doppione in anagrafica): il secondo avviso parte.
        old = _other(test_db_session, 1, nome="Doppione Srl")
        inv = _due(test_db_session, old, num="M/1", amount=100.0)
        kept = _other(test_db_session, 2, nome="Doppione S.r.l.")
        old.merged_into = kept.id
        inv.customer_id, inv.customer_name_raw, inv.customer_piva_raw = kept.id, kept.ragione_sociale, kept.partita_iva
        test_db_session.commit()
        assert reminder_sent(test_db_session, inv) is not None
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["out_of_scope"]) == (1, 0)
        assert mailer.sent[0]["to"] == ["amministrazione@cliente2.it"]
        # Catena di unioni (1 → 2 → 3) e anello difettoso: nessun ciclo infinito.
        third = _other(test_db_session, 3)
        kept.merged_into = third.id
        test_db_session.commit()
        assert fu._is_same_customer(test_db_session, old.id, third.id) is True
        assert fu._is_same_customer(test_db_session, third.id, old.id) is False
        third.merged_into = old.id
        test_db_session.commit()
        assert fu._is_same_customer(test_db_session, old.id, 999) is False
        assert fu._is_same_customer(test_db_session, None, kept.id) is False

    def test_invoice_rematched_or_detached_during_the_round_never_breaks_it(self, test_db_session):
        # Un sync a giro in corso tocca la fattura più vecchia del secondo
        # cliente: le sue altre fatture partono lo stesso, il giro non cade.
        alfa = _other(test_db_session, 1, nome="Alfa Srl")
        beta = _other(test_db_session, 2, nome="Beta Srl")
        gamma = _other(test_db_session, 3, nome="Gamma Srl")
        _due(test_db_session, alfa, num="A/1", due=date(2026, 11, 6))
        b_old = _due(test_db_session, beta, num="B/OLD", amount=10.0, due=date(2026, 11, 7))
        b_new = _due(test_db_session, beta, num="B/NEW", amount=20.0)
        g1 = _due(test_db_session, gamma, num="G/1", amount=30.0, due=date(2026, 11, 8))
        g2 = _due(test_db_session, gamma, num="G/2", amount=40.0)
        zeta = _other(test_db_session, 9, nome="Zeta Srl")
        _sync(test_db_session)

        class Moves(FakeMailer):
            def send(self, **kwargs):
                out = super().send(**kwargs)
                if self.calls == 1:   # mentre parte l'email di Alfa
                    test_db_session.query(Invoice).filter(Invoice.id == b_old.id).update({"customer_id": zeta.id})
                    test_db_session.query(Invoice).filter(Invoice.id == g1.id).update({"customer_id": None})
                return out

        mailer = Moves()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["failed"], summary["broken"]) == (3, 0, 0)
        by_to = {s["to"][0]: s for s in mailer.sent}
        assert "fattura n. B/NEW" in by_to["amministrazione@cliente2.it"]["subject"]
        assert "B/OLD" not in by_to["amministrazione@cliente2.it"]["text"]
        assert "fattura n. G/2" in by_to["amministrazione@cliente3.it"]["subject"]
        assert {r.invoice_id for r in _rows(test_db_session) if r.status == "sent"} >= {b_new.id, g2.id}
        assert not {b_old.id, g1.id} & {r.invoice_id for r in _rows(test_db_session)}

    def test_one_customer_blowing_up_never_stops_the_others(self, test_db_session, monkeypatch):
        alfa, beta = _other(test_db_session, 1), _other(test_db_session, 2)
        _due(test_db_session, alfa, num="A/1", due=date(2026, 11, 6))
        _due(test_db_session, beta, num="B/1")
        _sync(test_db_session)
        real = fu.plan_customer

        def flaky(session, invoices, window, mode, today, **kw):
            if kw.get("customer_id") == alfa.id:
                raise RuntimeError("piano rotto")
            return real(session, invoices, window, mode, today, **kw)

        monkeypatch.setattr(fu, "plan_customer", flaky)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["broken"]) == (1, 1) and mailer.sent[0]["to"] == ["amministrazione@cliente2.it"]
        assert not re_mod._reminder_lock.locked()

    def test_a_row_being_sent_by_another_process_is_not_taken(self, test_db_session, monkeypatch):
        # Stesso stato «sending» ma un ALTRO tentativo (altro orario): la riga
        # non si prende. Vale per due processi; nello stesso processo c'è il lock.
        from sqlalchemy.orm import Session as SASession
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        _sync(test_db_session)
        orphan = InvoiceFollowupEmail(invoice_id=inv.id, customer_id=c.id, due_date=DUE7, status="sending", mode="reale",
                                      attempted_at=datetime(2026, 11, 18, 8, 0))
        test_db_session.add(orphan)
        test_db_session.commit()
        real = fu.compose_followup

        def other_process_retries_first(*args, **kwargs):
            other = SASession(bind=test_db_session.get_bind())
            other.query(InvoiceFollowupEmail).filter_by(id=orphan.id).update({"attempted_at": datetime(2026, 11, 18, 9, 59)})
            other.commit()
            other.close()
            return real(*args, **kwargs)

        monkeypatch.setattr(fu, "compose_followup", other_process_retries_first)
        mailer = FakeMailer()
        out = retry_followup_row(test_db_session, orphan, mailer=mailer, cfg=_on_cfg())
        assert (out["sent"], out["already"]) == (0, 1) and mailer.calls == 0

    def test_error_after_the_send_never_turns_a_sent_email_into_a_failure(self, test_db_session, monkeypatch,
                                                                          caplog):
        c, a, b = self._pair(test_db_session)
        monkeypatch.setattr(fu, "_item", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("db caduto")))
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["invoices"], summary["failed"]) == (1, 2, 0) and summary["items"] == []
        # L'errore resta dentro il riepilogo: non arriva nemmeno al ripiego degli imprevisti.
        assert "esito non riportato nel riepilogo" in caplog.text and "errore imprevisto" not in caplog.text
        assert [(r.status, r.error, r.provider_message_id) for r in _rows(test_db_session)] == [("sent", None, "msg-1")] * 2
        # Anche se l'errore scappa fuori dopo la scrittura finale, il ripiego
        # non tocca le righe già inviate.
        c2, a2, b2 = self._pair(test_db_session, nome="Altro Srl", email="a@altro.it", partita_iva="00000000008")
        monkeypatch.setattr(fu, "_report", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("dopo l'invio")))
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["failed"]) == (1, 0) and mailer.calls == 2
        rows = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c2.id).all()
        assert [(r.status, r.error_kind, r.error) for r in rows] == [("sent", None, None)] * 2
        # In prova: stesso riguardo per le righe preparate.
        c3, a3, b3 = self._pair(test_db_session, nome="Terzo Srl", email="a@terzo.it", partita_iva="00000000009")
        summary = run_followups(test_db_session, cfg=_cfg(REMINDER_FOLLOWUP_ENABLED=True))
        rows = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c3.id).all()
        assert [(r.status, r.error_kind) for r in rows] == [("prepared", None)] * 2 and summary["failed"] == 0

    def test_doubtful_match_is_not_cited_among_other_overdue(self, test_db_session):
        c = _cust(test_db_session)
        ok = _due(test_db_session, c, num="OK/1", amount=100.0)
        doubtful = _inv(test_db_session, c, "DUBBIA/9", 999.0, date(2026, 10, 10),
                        customer_name_raw="Tutt'altra Azienda Spa", customer_piva_raw="99999999999")
        assert re_mod.skip_reason(doubtful, c) == "abbinamento_da_verificare"
        assert other_overdue_invoices(test_db_session, c.id, exclude_ids=[ok.id]) == []
        _sync(test_db_session)
        mailer = FakeMailer()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert "Risult" not in mailer.sent[0]["text"] and "999" not in mailer.sent[0]["text"]
        assert [n for n, _ in mailer.sent[0]["attachments"]] == ["copia_cortesia_fattura_OK_1.pdf"]
        # Verificata dall'operatore («Segna verificato»): torna tra le scadute certe.
        doubtful.audit_reviewed_at = datetime(2026, 11, 17, 9, 0)
        test_db_session.commit()
        assert [i.id for i in other_overdue_invoices(test_db_session, c.id, exclude_ids=[ok.id])] == [doubtful.id]

    def test_days_one_never_merges_an_invoice_due_today(self, test_db_session):
        # Con N = 1 il «giorno dopo» è oggi: una fattura che scade oggi non è scaduta.
        cfg = _on_cfg(REMINDER_FOLLOWUP_DAYS="1")
        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="A/1", amount=100.0, due=date(2026, 11, 17))   # scaduta da 1 giorno
        b = _due(test_db_session, c, num="B/2", amount=50.0, due=TODAY)                # scade oggi
        _sync(test_db_session)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=cfg)
        assert (summary["sent"], summary["invoices"], summary["merged_early"]) == (1, 1, 0)
        assert "è scaduta da 1 giorno (scadenza 17/11/2026)" in mailer.sent[0]["text"]
        assert "B/2" not in mailer.sent[0]["text"] and "0 giorni" not in mailer.sent[0]["text"]
        assert mailer.sent[0]["subject"] == "Sollecito di pagamento: fattura n. A/1 scaduta da 1 giorno (100,00 €)"
        assert {r.invoice_id for r in _rows(test_db_session)} == {a.id} and b.status == "open"
        assert [e["invoice_count"] for e in preview_followups(test_db_session, cfg=cfg) if e["will_process"]] == []

    def test_partial_payment_says_both_amounts(self, test_db_session):
        c = _cust(test_db_session, nome="Akiro Milano Srl")
        part = _due(test_db_session, c, amount=426.76, amount_due=300.0)   # acconto o nota di credito
        full = _due(test_db_session, c, num=N2, amount=493.68, due=DUE6)
        one = compose_followup(c, [part], [], TODAY)
        assert one["subject"] == "Sollecito di pagamento: fattura n. 2026/00001804/SAK scaduta da 7 giorni (300,00 €)"
        assert ("abbiamo rilevato che la fattura n. 2026/00001804/SAK di 426,76 € è scaduta da 7 giorni "
                "(scadenza 11/11/2026) e risultano ancora da saldare 300,00 €. Copia della fattura in allegato.") in one["text"]
        assert "non risulta ancora saldata" not in one["text"] and one["grand_total"] == 300.0
        two = compose_followup(c, [part, full], [], TODAY)
        assert two["subject"] == "Sollecito di pagamento: 2 fatture scadute (793,68 €)"
        assert ("- n. 2026/00001804/SAK di 426,76 €, da saldare 300,00 €, scaduta da 7 giorni (scadenza 11/11/2026)\n"
                "- n. 2026/00001812/SAK di 493,68 €, scaduta da 6 giorni (scadenza 12/11/2026)\n\n"
                "Totale: 793,68 €.") in two["text"]
        # Senza importo della fattura (dato mancante) vale il residuo, come sempre.
        part.amount = None
        assert "la fattura n. 2026/00001804/SAK di 300,00 € è scaduta" in compose_followup(c, [part], [], TODAY)["text"]

    def test_same_visible_number_twice(self, test_db_session):
        # Fattura e nota di debito con lo stesso numero: due righe, due file
        # con nomi diversi, il numero una volta sola nella causale.
        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="2026/00001804/SAK - Fattura", amount=100.0)
        b = _due(test_db_session, c, num="2026/00001804/SAK - Nota di debito", amount=20.0)
        msg = compose_followup(c, [a, b], [], TODAY)
        assert 'causale "Fattura 2026/00001804/SAK".' in msg["text"] and msg["text"].count("- n. 2026/00001804/SAK di") == 2
        names = [n for n, _ in fu.build_followup_attachments(c, [a, b], [], TODAY)]
        # Entrambi prendono l'id del documento: il nome non dipende dall'ordine.
        assert sorted(names) == sorted([f"copia_cortesia_fattura_2026_00001804_SAK_{a.id}.pdf",
                                        f"copia_cortesia_fattura_2026_00001804_SAK_{b.id}.pdf"])
        assert sorted(n for n, _ in fu.build_followup_attachments(c, [b, a], [], TODAY)) == sorted(names)
        by_name = dict(fu.build_followup_attachments(c, [a, b], [], TODAY))
        assert "Nota di debito" in _pdf_strings(by_name[f"copia_cortesia_fattura_2026_00001804_SAK_{b.id}.pdf"]) or (
            "20,00" in _pdf_strings(by_name[f"copia_cortesia_fattura_2026_00001804_SAK_{b.id}.pdf"]))
        assert fu._unique_names(["a.pdf", "a.pdf", "b", "c", "a.pdf"], [1, 2, 3, 4, 5]) == [
            "a_1.pdf", "a_2.pdf", "b", "c", "a_5.pdf"]
        assert fu._unique_names(["x", "x"], [7, 8]) == ["x_7", "x_8"]
        # Caso di scuola: un nome che coincide col nome «con l'id» di un altro resta comunque unico.
        assert fu._unique_names(["a.pdf", "a.pdf", "a_5.pdf"], [5, 6, 7]) == ["a_5.pdf", "a_6.pdf", "a_5_2.pdf"]

    def test_refused_email_is_retried_whole_at_the_next_round(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        refused = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        assert run_followups(test_db_session, mailer=refused, cfg=_on_cfg())["failed"] == 1
        assert [(r.status, r.error_kind) for r in _rows(test_db_session)] == [("failed", "validation")] * 2
        second = run_followups(test_db_session, mailer=refused, cfg=_on_cfg())  # rifiuto certo: ritento da solo
        assert (second["sent"], second["invoices"]) == (1, 2) and len(refused.sent) == 1
        assert "2 fatture scadute" in refused.sent[0]["subject"]
        assert {r.status for r in _rows(test_db_session)} == {"sent"} and len(_rows(test_db_session)) == 2

    def test_unexpected_error_before_sending_is_visible_and_retried_by_the_round(self, test_db_session, monkeypatch):
        c, a, b = self._pair(test_db_session)
        other = _due(test_db_session, _other(test_db_session, 9), num="X/1")
        real = fu.compose_followup

        def flaky(customer, invoices, overdue, today=None):
            if customer.id == c.id:
                raise RuntimeError("boom")
            return real(customer, invoices, overdue, today)

        monkeypatch.setattr(fu, "compose_followup", flaky)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["failed"], summary["sent"], summary["invoices"]) == (1, 1, 1)
        rows = {r.invoice_id: r for r in _rows(test_db_session)}
        # Nulla era partito (errore prima della riserva): righe visibili, non «incerte».
        assert [(rows[i.id].status, rows[i.id].error_kind) for i in (a, b)] == [("failed", "not_attempted")] * 2
        assert "boom" in rows[a.id].error and fu._is_uncertain(rows[a.id]) is False
        assert rows[other.id].status == "sent"
        monkeypatch.setattr(fu, "compose_followup", real)
        second = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())   # il giro le riprova da solo
        assert (second["sent"], second["invoices"]) == (1, 2) and "2 fatture scadute" in mailer.sent[-1]["subject"]

    def test_unexpected_error_after_the_reservation_is_uncertain(self, test_db_session, monkeypatch):
        # L'eccezione arriva a invio avviato (righe già riservate): l'email
        # potrebbe essere partita → ferme finché un operatore non le ritenta.
        c, a, b = self._pair(test_db_session)

        class Explodes(FakeMailer):
            def send(self, **kwargs):
                self.calls += 1
                self.keys.append(kwargs["idempotency_key"])
                raise RuntimeError("processo interrotto a metà")   # non è un MailSendError

        mailer = Explodes()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        rows = _rows(test_db_session)
        assert summary["failed"] == 1 and [(r.status, r.error_kind) for r in rows] == [("failed", "other")] * 2
        assert {r.idempotency_key for r in rows} == {mailer.keys[0]} and fu._is_uncertain(rows[0])
        plain = FakeMailer()
        assert run_followups(test_db_session, mailer=plain, cfg=_on_cfg())["sent"] == 0 and plain.calls == 0
        out = retry_followup_row(test_db_session, rows[0], mailer=plain, cfg=_on_cfg())
        assert (out["sent"], out["invoices"]) == (1, 2) and plain.sent[0]["idempotency_key"] == mailer.keys[0]

    def test_error_after_the_send_but_before_the_final_write_is_uncertain_and_never_duplicates(
            self, test_db_session, monkeypatch):
        # L'email è partita ma la scrittura finale salta: le righe (riservate
        # da questo tentativo) restano incerte; il ritento riusa la chiave e
        # Resend risponde 409 → nessun doppione, decide un operatore.
        c, a, b = self._pair(test_db_session)
        resend = self._Resend(lost=1)
        run_followups(test_db_session, mailer=resend, cfg=_on_cfg())               # 1° tentativo: timeout, non arrivata? arrivata.
        rows = _rows(test_db_session)
        resend2 = self._Resend(lost=0)                                             # un altro Resend «pulito»: non ha mai visto la chiave
        monkeypatch.setattr(fu, "_release_siblings", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("db caduto")))
        out = retry_followup_row(test_db_session, rows[0], mailer=resend2, cfg=_on_cfg())
        assert len(resend2.delivered) == 1 and out["sent"] == 0 and out["failed"] == 1
        assert [(r.status, r.error_kind) for r in _rows(test_db_session)] == [("failed", "other")] * 2
        assert {r.idempotency_key for r in _rows(test_db_session)} == {resend2.keys[0]} == {resend.keys[0]}
        monkeypatch.undo()
        for target in ("format", "reminder_email", "followup_email"):
            monkeypatch.setattr(f"backend.engine.{target}.datetime", _FixedDatetime)
        assert run_followups(test_db_session, mailer=resend2, cfg=_on_cfg())["sent"] == 0
        out = retry_followup_row(test_db_session, _rows(test_db_session)[0], mailer=resend2, cfg=_on_cfg())
        assert out["failed"] == 1 and len(resend2.delivered) == 1                   # 409: non riparte
        assert {r.error_kind for r in _rows(test_db_session)} == {"conflict"}

    def test_an_error_before_the_reservation_never_changes_existing_rows(self, test_db_session, monkeypatch):
        # Errore prima di qualunque invio: lo stato di prima delle righe è
        # ancora vero e non si tocca (un incerto resta incerto, un rifiuto
        # certo resta ritentabile dal giro, un invio in test resta tale).
        real = fu.compose_followup
        boom = lambda *x, **k: (_ for _ in ()).throw(RuntimeError("boom"))   # noqa: E731
        # a) riga incerta ritentata a mano
        c, a, b = self._pair(test_db_session)
        timeout = FakeMailer(fail_times=1, kind=ERROR_NETWORK)
        run_followups(test_db_session, mailer=timeout, cfg=_on_cfg())
        before = [(r.status, r.error_kind, r.idempotency_key, r.error) for r in _rows(test_db_session)]
        monkeypatch.setattr(fu, "compose_followup", boom)
        out = retry_followup_row(test_db_session, _rows(test_db_session)[0], mailer=timeout, cfg=_on_cfg())
        assert out["failed"] == 1 and timeout.calls == 1
        assert [(r.status, r.error_kind, r.idempotency_key, r.error) for r in _rows(test_db_session)] == before
        # b) rifiuto certo: dopo l'imprevisto il giro lo ritenta ancora da solo
        c2 = _other(test_db_session, 2)
        _due(test_db_session, c2, num="V/1")
        monkeypatch.setattr(fu, "compose_followup", real)
        refused = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        run_followups(test_db_session, mailer=refused, cfg=_on_cfg())
        row = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c2.id).one()
        monkeypatch.setattr(fu, "compose_followup", boom)
        run_followups(test_db_session, mailer=refused, cfg=_on_cfg())
        assert (row.status, row.error_kind) == ("failed", "validation")
        monkeypatch.setattr(fu, "compose_followup", real)
        assert run_followups(test_db_session, mailer=refused, cfg=_on_cfg())["sent"] == 1
        # c) inviata in TEST, poi un giro reale che si rompe prima dell'invio: resta «inviata in test»
        c3 = _other(test_db_session, 3)
        _due(test_db_session, c3, num="T/1")
        test_cfg = _on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        mailer = FakeMailer()
        run_followups(test_db_session, mailer=mailer, cfg=test_cfg)
        row = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c3.id).one()
        monkeypatch.setattr(fu, "compose_followup", boom)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        # Non resta «inviata (test)» in silenzio: diventa visibile, e il giro la riprende da solo.
        assert (row.status, row.mode, row.error_kind) == ("failed", "test", "not_attempted")
        monkeypatch.setattr(fu, "compose_followup", real)
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert mailer.sent[-1]["to"] == ["amministrazione@cliente3.it"]
        # d) saltata (cliente senza email), poi l'email arriva ma il giro si rompe: visibile, non «saltata» per sempre
        c4 = _other(test_db_session, 4, email="")
        _due(test_db_session, c4, num="S/1")
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c4.id).one()
        assert (row.status, row.skip_reason) == ("skipped", "senza_email")
        c4.email = "amministrazione@cliente4.it"
        test_db_session.commit()
        monkeypatch.setattr(fu, "compose_followup", boom)
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (row.status, row.error_kind) == ("failed", "not_attempted")
        assert row.id in [r.id for r, _i, _c in fu.stuck_rows(test_db_session)]   # ora è tra le «da gestire»
        monkeypatch.setattr(fu, "compose_followup", real)
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["sent"] == 1 and row.status == "sent"

    def test_a_row_sent_for_real_never_becomes_retryable(self, test_db_session, monkeypatch):
        # Cintura: qualunque strada porti al ripiego degli imprevisti (qui un
        # tentativo in test su una riga già inviata in REALE, come potrebbe
        # fare un secondo processo in un'altra modalità), una riga inviata
        # davvero al cliente resta inviata.
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        _sync(test_db_session)
        mailer = FakeMailer()
        assert run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        row = _rows(test_db_session)[0]
        monkeypatch.setattr(fu, "compose_followup", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        summary = fu._summary(MODE_TEST, [], None)
        fu._process_group_safely(test_db_session, c, [inv], key="k", mode=MODE_TEST, mailer=mailer,
                                 cfg=_on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com"), summary=summary,
                                 existing={inv.id: row}, today=TODAY)
        test_db_session.refresh(row)
        assert (row.status, row.mode, row.error_kind) == ("sent", "reale", None) and mailer.calls == 1

    def test_test_mode_outcomes_never_block_the_real_email(self, test_db_session):
        # In prova/test il cliente non riceve nulla: in reale ogni riga di
        # test è un invio nuovo, anche se era incerta o in conflitto con Resend.
        test_cfg = _on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        # a) timeout in test (ma accettata), «Riprova» in test → 409 → conflitto in test
        c1 = _other(test_db_session, 1)
        _due(test_db_session, c1, num="T/1")
        _sync(test_db_session)
        resend = self._Resend()
        run_followups(test_db_session, mailer=resend, cfg=test_cfg)
        row = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c1.id).one()
        retry_followup_row(test_db_session, row, mailer=resend, cfg=test_cfg)
        assert (row.status, row.mode, row.error_kind) == ("failed", "test", "conflict")
        real = run_followups(test_db_session, mailer=resend, cfg=_on_cfg())
        assert real["sent"] == 1 and resend.delivered[-1]["to"] == ["amministrazione@cliente1.it"]
        assert (row.status, row.mode) == ("sent", "reale") and len(set(resend.keys)) == 2
        # b) timeout in test, poi «Riprova» direttamente in REALE: chiave nuova (non quella del test), parte
        c2 = _other(test_db_session, 2)
        _due(test_db_session, c2, num="T/2")
        resend2 = self._Resend()
        run_followups(test_db_session, mailer=resend2, cfg=test_cfg)
        row2 = test_db_session.query(InvoiceFollowupEmail).filter_by(customer_id=c2.id).one()
        assert (row2.status, row2.mode, row2.error_kind) == ("failed", "test", "network")
        out = retry_followup_row(test_db_session, row2, mailer=resend2, cfg=_on_cfg())
        assert out["sent"] == 1 and resend2.keys[0] != resend2.keys[1]
        assert resend2.delivered[-1]["to"] == ["amministrazione@cliente2.it"] and (row2.status, row2.mode) == ("sent", "reale")
        # c) riga interrotta a metà in test: in reale il giro la manda da solo
        c3 = _other(test_db_session, 3)
        inv3 = _due(test_db_session, c3, num="T/3")
        orphan = InvoiceFollowupEmail(invoice_id=inv3.id, customer_id=c3.id, due_date=DUE7, status="sending", mode="test")
        test_db_session.add(orphan)
        test_db_session.commit()
        assert fu._needs_processing(orphan, MODE_TEST) == (False, "invio interrotto: serve «Riprova»")
        assert fu._needs_processing(orphan, MODE_REALE) == (True, "invio reale dopo il test")
        plain = FakeMailer()
        assert run_followups(test_db_session, mailer=plain, cfg=_on_cfg())["sent"] == 1 and orphan.status == "sent"
        # In reale, invece, un incerto REALE resta fermo.
        orphan.status, orphan.mode = "sending", "reale"
        assert fu._needs_processing(orphan, MODE_REALE)[0] is False

    def test_a_split_retry_does_not_burn_the_key_of_the_invoice_left_out(self, test_db_session):
        # {A, B} fallisce senza mai arrivare a Resend. Al ritento B è esclusa
        # (promessa): A parte da sola con la chiave del primo tentativo. Poi B
        # torna sollecitabile: il suo ritento NON deve prendere un 409 per
        # l'email di A (che di B non parlava).
        class DownOnce(TestOneEmailPerCustomer._Resend):
            def __init__(self):
                super().__init__(lost=0)
                self.down = 1

            def send(self, **kwargs):
                if self.down:
                    self.down -= 1
                    self.calls += 1
                    self.keys.append(kwargs["idempotency_key"])
                    raise MailSendError("Resend non raggiungibile: ConnectError", kind="network")
                return super().send(**kwargs)

        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="A/1", amount=100.0)
        b = _due(test_db_session, c, num="B/2", amount=200.0, due=date(2026, 11, 10))
        # Un altro cliente con un invio fermo suo (un'altra chiave): non c'entra e non va toccato.
        stranger = _due(test_db_session, _other(test_db_session, 7), num="Z/9")
        foreign = InvoiceFollowupEmail(invoice_id=stranger.id, customer_id=stranger.customer_id, due_date=DUE7,
                                       status="failed", mode="reale", error_kind="network", idempotency_key="chiave-di-un-altro")
        test_db_session.add(foreign)
        test_db_session.commit()
        _sync(test_db_session)
        mailer = DownOnce()
        run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        row_a = test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=a.id).one()
        row_b = test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=b.id).one()
        assert row_a.idempotency_key == row_b.idempotency_key == mailer.keys[0]
        b.status = "promised"
        test_db_session.commit()
        assert retry_followup_row(test_db_session, row_a, mailer=mailer, cfg=_on_cfg())["sent"] == 1
        assert mailer.keys[1] == mailer.keys[0]                      # A: continua il primo tentativo
        assert (row_b.status, row_b.error_kind, row_b.idempotency_key) == ("failed", "network", None)
        assert row_b.key_first_used_at is None                       # senza chiave non c'è un orologio da rispettare
        assert foreign.idempotency_key == "chiave-di-un-altro"       # le righe con un'altra chiave restano com'erano
        b.status = "open"
        test_db_session.commit()
        out = retry_followup_row(test_db_session, row_b, mailer=mailer, cfg=_on_cfg())
        assert out["sent"] == 1 and (row_b.status, row_b.error_kind) == ("sent", None)
        assert mailer.keys[2].startswith(_idem(c, [b])) and mailer.keys[2] != mailer.keys[0]
        assert [d["subject"].split(" scaduta")[0] for d in mailer.delivered] == [
            "Sollecito di pagamento: fattura n. A/1", "Sollecito di pagamento: fattura n. B/2"]

    def test_pdf_failure_marks_every_invoice_and_is_retried(self, test_db_session, monkeypatch):
        c, a, b = self._pair(test_db_session)
        real = fu.build_followup_attachments
        monkeypatch.setattr(fu, "build_followup_attachments", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("font")))
        mailer = FakeMailer()
        first = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (first["failed"], first["sent"]) == (1, 0) and mailer.calls == 0
        assert [(r.status, r.error_kind) for r in _rows(test_db_session)] == [("failed", "pdf")] * 2
        monkeypatch.setattr(fu, "build_followup_attachments", real)
        second = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())  # nessun invio tentato: ritento sicuro
        assert (second["sent"], second["invoices"]) == (1, 2) and mailer.calls == 1

    def test_reservation_conflict_sends_nothing(self, test_db_session, monkeypatch):
        # Un altro giro ha già riservato UNA delle due fatture e questo giro
        # non l'ha vista: il vincolo unico ferma l'intera email.
        c, a, b = self._pair(test_db_session)
        taken = InvoiceFollowupEmail(invoice_id=b.id, customer_id=c.id, due_date=DUE6, status="sending", mode="reale")
        test_db_session.add(taken)
        test_db_session.commit()
        monkeypatch.setattr(fu, "_existing_row", lambda session, inv: None)
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (summary["sent"], summary["failed"], summary["already"]) == (0, 0, 2) and mailer.calls == 0
        rows = _rows(test_db_session)
        assert [(r.invoice_id, r.status, r.group_key) for r in rows] == [(b.id, "sending", None)]

    def test_prova_then_test_then_real_is_always_one_email(self, test_db_session):
        c, a, b = self._pair(test_db_session, tags="B2B, izzo")
        prova = run_followups(test_db_session, cfg=_cfg(REMINDER_FOLLOWUP_ENABLED=True))
        assert (prova["mode"], prova["prepared"], prova["sent"], prova["invoices"]) == (MODE_PROVA, 1, 0, 2)
        rows = _rows(test_db_session)
        assert {r.status for r in rows} == {"prepared"} and len({r.group_key for r in rows}) == 1
        assert rows[0].attachments_json == ["copia_cortesia_fattura_A_1.pdf", "copia_cortesia_fattura_B_2.pdf"]
        assert run_followups(test_db_session, cfg=_cfg(REMINDER_FOLLOWUP_ENABLED=True))["prepared"] == 0
        mailer = FakeMailer()
        test_cfg = _on_cfg(REMINDER_EMAIL_TEST_TO="lorenzo@example.com")
        test = run_followups(test_db_session, mailer=mailer, cfg=test_cfg)
        assert (test["mode"], test["sent"], test["invoices"]) == (MODE_TEST, 1, 2)
        assert mailer.sent[0]["to"] == ["lorenzo@example.com"] and mailer.sent[0]["cc"] is None
        assert mailer.sent[0]["idempotency_key"].startswith(_idem(c, [a, b], mode="test"))
        assert mailer.keys[0] != mailer.sent[0]["idempotency_key"] if len(mailer.keys) > 1 else True
        assert {(r.to_email, r.original_to) for r in _rows(test_db_session)} == {
            ("lorenzo@example.com", "amministrazione@shabu.it")}
        real = run_followups(test_db_session, mailer=mailer, cfg=_on_cfg())
        assert (real["sent"], real["invoices"]) == (1, 2) and mailer.calls == 2
        assert mailer.sent[1]["to"] == ["amministrazione@shabu.it"]
        assert mailer.sent[1]["cc"] == ["a.izzo@sakecompany.com", "fatture@sakecompany.com"]
        assert {r.status for r in _rows(test_db_session)} == {"sent"} and len(_rows(test_db_session)) == 2

    def test_a_skipped_invoice_loses_its_group(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        refused = FakeMailer(fail_times=1, kind=ERROR_VALIDATION)
        run_followups(test_db_session, mailer=refused, cfg=_on_cfg())
        c.recovery_status = "waiting"
        test_db_session.commit()
        summary = run_followups(test_db_session, mailer=refused, cfg=_on_cfg())
        # A (in finestra) è registrata come saltata; B (scadenza prossima) non viene toccata.
        assert (summary["sent"], summary["skipped"]) == (0, 1) and refused.calls == 1
        row_a, row_b = _rows(test_db_session)
        assert (row_a.status, row_a.skip_reason, row_a.group_key) == ("skipped", "in_attesa", None)
        assert (row_b.status, row_b.error_kind) == ("failed", "validation")

    # ── Anteprima e invio di prova ──

    def test_preview_has_one_row_per_email(self, test_db_session):
        alfa = _other(test_db_session, 1, nome="Alfa Srl", tags="B2B, maggioni")
        a = _due(test_db_session, alfa, num="A/1", amount=100.0)
        b = _due(test_db_session, alfa, num="A/2", amount=200.0, due=DUE6)
        _inv(test_db_session, alfa, "A/OLD", 40.0, date(2026, 10, 1))
        beta = _other(test_db_session, 2, nome="Beta Srl")
        _due(test_db_session, beta, num="B/1", due=DUE6)                       # da sola: oggi non c'è
        gamma = _other(test_db_session, 3, nome="Gamma Srl")
        g1 = _due(test_db_session, gamma, num="G/1", amount=10.0, due=date(2026, 11, 10))
        g2 = _due(test_db_session, gamma, num="G/2", amount=20.0, due=date(2026, 11, 10), status="promised")
        _sync(test_db_session)
        cfg = _on_cfg()
        entries = preview_followups(test_db_session, cfg=cfg)
        assert _rows(test_db_session) == []
        assert [(e["ragione_sociale"], e["invoice_count"], e["will_process"], e["skip_reason"]) for e in entries] == [
            ("Gamma Srl", 1, True, None), ("Gamma Srl", 1, False, "promessa"), ("Alfa Srl", 2, True, None)]
        email = entries[2]
        assert [(i["invoice_id"], i["invoice_number"], i["days_overdue"], i["early"], i["amount_due"])
                for i in email["invoices"]] == [(a.id, "A/1", 7, False, 100.0), (b.id, "A/2", 6, True, 200.0)]
        assert all(i["reminder_sent_at"] for i in email["invoices"])
        assert (email["invoice_id"], email["amount_due"], email["due_date"], email["why"]) == (a.id, 300.0, "2026-11-11", "nuova")
        assert (email["overdue_count"], email["overdue_total"], email["grand_total"]) == (1, 40.0, 340.0)
        assert email["subject"] == "Sollecito di pagamento: 2 fatture scadute (300,00 €)"
        assert email["cc"] == ["g.maggioni@sakecompany.com", "fatture@sakecompany.com"]
        assert (entries[1]["invoice_id"], entries[1]["text"], entries[1]["skip_label"]) == (
            g2.id, None, "Pagamento promesso (stato impostato dall'operatore)")
        assert entries[0]["invoice_id"] == g1.id and entries[0]["invoices"][0]["early"] is False
        # Quello che l'anteprima promette è quello che parte: stesse email, stessi testi.
        mailer = FakeMailer()
        summary = run_followups(test_db_session, mailer=mailer, cfg=cfg)
        assert (summary["sent"], summary["invoices"], summary["skipped"]) == (2, 3, 1)
        assert sorted(s["text"] for s in mailer.sent) == sorted(e["text"] for e in entries if e["will_process"])
        assert sorted(s["subject"] for s in mailer.sent) == sorted(e["subject"] for e in entries if e["will_process"])
        # Dopo il giro: nulla da inviare; le fatture inviate risultano una per riga.
        after = preview_followups(test_db_session, cfg=cfg)
        assert [e["will_process"] for e in after] == [False] * len(after)
        assert sorted((e["invoice_number"], e["already"]) for e in after) == [
            ("A/1", "sent"), ("G/1", "sent"), ("G/2", "skipped")]

    def test_preview_says_why_an_email_is_sent_again(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        run_followups(test_db_session, cfg=_cfg(REMINDER_FOLLOWUP_ENABLED=True))  # prova: preparate
        entry = preview_followups(test_db_session, cfg=_on_cfg())[0]
        assert (entry["will_process"], entry["already"], entry["already_mode"], entry["why"]) == (
            True, "prepared", "prova", "invio dopo la prova")
        assert entry["invoice_count"] == 2

    def test_test_send_is_the_same_single_email(self, test_db_session):
        c, a, b = self._pair(test_db_session, tags="B2B, scotti")
        _inv(test_db_session, c, "OLD/1", 40.0, date(2026, 10, 1))
        off = _cfg(REMINDER_EMAIL_ENABLED=True, RESEND_API_KEY="re_x")
        preview = preview_followups(test_db_session, cfg=off)[0]
        for start in (a, b):  # da qualunque fattura dell'email si parta
            mailer = FakeMailer()
            out = send_followup_test(test_db_session, start.id, "lorenzo@example.com", mailer=mailer, cfg=off)
            assert out["invoice_numbers"] == ["A/1", "B/2"] and out["invoice_id"] == start.id
            sent = mailer.sent[0]
            assert sent["subject"] == "[PROVA] Sollecito di pagamento: 2 fatture scadute (300,00 €)"
            assert sent["text"] == preview["text"] and "cc" not in sent and "idempotency_key" not in sent
            assert [n for n, _ in sent["attachments"]] == out["attachments"] == [
                "copia_cortesia_fattura_A_1.pdf", "copia_cortesia_fattura_B_2.pdf", "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        assert _rows(test_db_session) == []
        assert [i.id for i in email_group_of(test_db_session, b, cfg=off)] == [a.id, b.id]

    def test_test_send_falls_back_to_the_single_invoice(self, test_db_session):
        c, a, b = self._pair(test_db_session)
        old = _due(test_db_session, c, num="OLD/1", amount=40.0, due=date(2026, 10, 29))  # fuori dal giro di oggi
        cfg = _on_cfg()
        mailer = FakeMailer()
        out = send_followup_test(test_db_session, old.id, "lorenzo@example.com", mailer=mailer, cfg=cfg)
        assert out["invoice_numbers"] == ["OLD/1"]
        assert "è scaduta da 20 giorni (scadenza 29/10/2026)" in mailer.sent[0]["text"]
        assert "altre 2 fatture per un totale di 300,00 €" in mailer.sent[0]["text"]
        # Nel weekend non c'è un giro: la fattura da sola, coi giorni di quel giorno.
        saturday = _at(date(2026, 11, 21))
        out = send_followup_test(test_db_session, a.id, "lorenzo@example.com", mailer=mailer, cfg=cfg, now_utc=saturday)
        assert out["invoice_numbers"] == ["A/1"] and "è scaduta da 10 giorni" in mailer.sent[1]["text"]
        assert [i.id for i in email_group_of(test_db_session, a, now_utc=saturday, cfg=cfg)] == [a.id]
        # Già inviata nel giro di oggi: non fa più parte di un'email di oggi.
        run_followups(test_db_session, mailer=FakeMailer(), cfg=cfg)
        out = send_followup_test(test_db_session, b.id, "lorenzo@example.com", mailer=mailer, cfg=cfg)
        assert out["invoice_numbers"] == ["B/2"]
        # Cliente senza email: la prova parte lo stesso (va a un indirizzo interno), con tutte le sue fatture.
        noemail = _other(test_db_session, 7, email="")
        x = _due(test_db_session, noemail, num="X/1")
        _due(test_db_session, noemail, num="X/2")
        out = send_followup_test(test_db_session, x.id, "lorenzo@example.com", mailer=mailer, cfg=cfg)
        assert out["invoice_numbers"] == ["X/1", "X/2"]
        assert len(_rows(test_db_session)) == 2  # solo le due righe del giro vero


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

    def test_one_customer_blowing_up_never_breaks_the_preview(self, test_db_session, monkeypatch):
        alfa, beta = _other(test_db_session, 1), _other(test_db_session, 2)
        _due(test_db_session, alfa, num="A/1", due=date(2026, 11, 6))
        _due(test_db_session, beta, num="B/1")
        real = fu.plan_customer

        def flaky(session, invoices, window, mode, today, **kw):
            if kw.get("customer_id") == alfa.id:
                raise RuntimeError("piano rotto")
            return real(session, invoices, window, mode, today, **kw)

        monkeypatch.setattr(fu, "plan_customer", flaky)
        assert [e["invoice_number"] for e in preview_followups(test_db_session, cfg=_on_cfg())] == ["B/1"]

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
        # Una riga interrotta senza chiave registrata non ha nulla da riusare: chiave nuova, che resta sulla riga.
        assert mailer.sent[0]["idempotency_key"].startswith(_idem(c, [inv])) and (
            orphan.idempotency_key == mailer.sent[0]["idempotency_key"])
        assert orphan.group_key == group_key(c.id, [inv], TODAY)
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
        assert "un'altra fattura per 100,00 €. Totale da saldare: 526,76 €." in sent["text"]
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
        assert (data["near_days"], data["merge_until"]) == (1, "2026-11-12")
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

    def test_one_email_with_two_invoices_in_registry_detail_attachments_and_log(self, test_client, test_db_session,
                                                                                monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session, tags="B2B, scotti")
        a = _due(test_db_session, c, num="A/1", amount=100.0)
        b = _due(test_db_session, c, num="B/2", amount=200.0, due=DUE6)
        _inv(test_db_session, c, "OLD/1", 40.0, date(2026, 10, 1))
        _sync(test_db_session)
        created = self._spy(monkeypatch)
        status = test_client.get("/api/reminders/followup").json()
        assert len(status["preview"]) == 1 and status["preview"][0]["invoice_count"] == 2
        assert [i["early"] for i in status["preview"][0]["invoices"]] == [False, True]
        run = test_client.post("/api/reminders/followup/run", json={"expected_mode": "reale"}).json()
        assert (run["sent"], run["invoices"], run["merged_early"]) == (1, 2, 1)
        assert len(created[0].sent) == 1  # UNA email
        recent = test_client.get("/api/reminders/followup").json()["recent"]
        assert len(recent) == 2 and {r["status"] for r in recent} == {"sent"}  # una riga per fattura
        by_inv = {r["invoice_id"]: r for r in recent}
        assert by_inv[a.id]["group_size"] == by_inv[b.id]["group_size"] == 2
        assert by_inv[a.id]["group_key"] == by_inv[b.id]["group_key"] != None  # noqa: E711
        assert [(m["invoice_id"], m["invoice_number"], m["due_date"], m["amount_due"])
                for m in by_inv[a.id]["group_invoices"]] == [(b.id, "B/2", "2026-11-12", 200.0)]
        assert [m["invoice_number"] for m in by_inv[b.id]["group_invoices"]] == ["A/1"]
        names = ["copia_cortesia_fattura_A_1.pdf", "copia_cortesia_fattura_B_2.pdf",
                 "riepilogo_scadute_Shabu_Casatenovo.pdf"]
        assert by_inv[a.id]["attachments"] == by_inv[b.id]["attachments"] == names
        for row in recent:  # da qualunque riga: stesso testo, stessi allegati
            detail = test_client.get(f"/api/reminders/followup/{row['id']}").json()
            assert "- n. A/1 di 100,00 €, scaduta da 7 giorni" in detail["body_text"]
            assert "- n. B/2 di 200,00 €, scaduta da 6 giorni" in detail["body_text"]
            assert detail["group_size"] == 2 and len(detail["group_invoices"]) == 1
            for index, name in enumerate(names):
                pdf = test_client.get(f"/api/reminders/followup/{row['id']}/attachments/{index}")
                assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
                assert name in pdf.headers["content-disposition"]
            assert test_client.get(f"/api/reminders/followup/{row['id']}/attachments/3").status_code == 404
            # Riepilogo rigenerato: le due dell'email + l'altra scaduta, ognuna una volta sola.
            summary_pdf = _pdf_strings(test_client.get(f"/api/reminders/followup/{row['id']}/attachments/2").content)
            assert "340,00 EUR" in summary_pdf and summary_pdf.count("B/2") == 1 and summary_pdf.count("A/1") == 1
        log = test_client.get("/api/reminders/email/log", params={"customer_id": c.id}).json()["items"]
        followups = [i for i in log if i["kind"] == "followup"]
        reminders = [i for i in log if i["kind"] == "promemoria"]
        assert len(followups) == 2 and all(i["group_size"] == 2 and len(i["group_invoices"]) == 1 for i in followups)
        assert len(reminders) == 2
        assert all((i["group_size"], i["group_invoices"], i["group_key"]) == (1, [], None) for i in reminders)
        assert test_client.get("/api/reminders/email").json()["recent"][0]["group_size"] == 1

    def test_retry_endpoint_resends_the_whole_email(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        _due(test_db_session, c, num="A/1", amount=100.0)
        _due(test_db_session, c, num="B/2", amount=200.0, due=DUE6)
        _sync(test_db_session)
        self._spy(monkeypatch, fail_times=1, kind=ERROR_NETWORK)
        assert test_client.post("/api/reminders/followup/run").json()["failed"] == 1
        rows = _rows(test_db_session)
        assert [r.status for r in rows] == ["failed", "failed"]
        created = self._spy(monkeypatch)
        retried = test_client.post(f"/api/reminders/followup/{rows[1].id}/retry", json={"expected_mode": "reale"})
        assert retried.status_code == 200
        assert (retried.json()["sent"], retried.json()["invoices"]) == (1, 2)
        assert len(created[0].sent) == 1 and "2 fatture scadute" in created[0].sent[0]["subject"]
        recent = test_client.get("/api/reminders/followup").json()["recent"]
        assert {r["status"] for r in recent} == {"sent"} and len(recent) == 2

    def test_row_without_a_group_is_its_own_email(self, test_client, test_db_session, monkeypatch):
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        inv = _due(test_db_session, c)
        other = _due(test_db_session, c, num=N2, amount=10.0)
        lone = InvoiceFollowupEmail(invoice_id=inv.id, customer_id=c.id, due_date=DUE7, status="failed", mode="reale",
                                    error_kind="network", subject="x")
        test_db_session.add(lone)
        test_db_session.commit()
        assert [i.id for i in row_invoices(test_db_session, lone)] == [inv.id]
        assert group_members(test_db_session, [lone]) == {}
        item = test_client.get(f"/api/reminders/followup/{lone.id}").json()
        assert (item["group_key"], item["group_size"], item["group_invoices"]) == (None, 1, [])
        assert test_client.get(f"/api/reminders/followup/{lone.id}/attachments/0").status_code == 200
        # Senza altre scadute citabili oltre a «other» (che è una scaduta certa): c'è il riepilogo.
        assert test_client.get(f"/api/reminders/followup/{lone.id}/attachments/1").status_code == 200
        assert test_client.get(f"/api/reminders/followup/{lone.id}/attachments/2").status_code == 404
        assert other.id != inv.id

    def test_stuck_rows_never_drop_out_of_sight(self, test_client, test_db_session, monkeypatch):
        # Le righe ferme restano in un elenco a parte, qualunque età abbiano e
        # per quanto il registro si riempia: è lì che l'operatore le ritenta.
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="FERMA/1", amount=100.0)
        _sync(test_db_session)
        self._spy(monkeypatch, fail_times=1, kind=ERROR_NETWORK)
        assert test_client.post("/api/reminders/followup/run").json()["failed"] == 1
        paid = _due(test_db_session, _other(test_db_session, 2), num="PAGATA/1")
        test_db_session.add(InvoiceFollowupEmail(invoice_id=paid.id, customer_id=paid.customer_id, due_date=DUE7,
                                                 status="failed", mode="reale", error_kind="network"))
        paid.status, paid.amount_due = "paid", 0.0
        conflict = _due(test_db_session, _other(test_db_session, 3), num="CONFLITTO/1")
        test_db_session.add(InvoiceFollowupEmail(invoice_id=conflict.id, customer_id=conflict.customer_id, due_date=DUE7,
                                                 status="failed", mode="reale", error_kind="conflict",
                                                 attempted_at=datetime(2026, 11, 18, 9, 0)))
        for n in range(40):   # il registro si riempie di invii riusciti più recenti
            inv = _due(test_db_session, _other(test_db_session, 100 + n), num=f"OK/{n}")
            test_db_session.add(InvoiceFollowupEmail(invoice_id=inv.id, customer_id=inv.customer_id, due_date=DUE7,
                                                     status="sent", mode="reale",
                                                     created_at=datetime(2026, 12, 1, 12, n)))
        test_db_session.commit()
        data = test_client.get("/api/reminders/followup").json()
        assert len(data["recent"]) == 30 and {r["status"] for r in data["recent"]} == {"sent"}
        stuck = {r["invoice_number"]: r for r in data["stuck"]}
        assert set(stuck) == {"FERMA/1", "CONFLITTO/1"}            # la pagata non è più «da gestire»
        assert (stuck["FERMA/1"]["needs_manual_retry"], stuck["FERMA/1"]["retry_needs_confirmation"]) == (True, False)
        assert (stuck["CONFLITTO/1"]["needs_manual_retry"], stuck["CONFLITTO/1"]["retry_needs_confirmation"]) == (True, True)
        assert stuck["FERMA/1"]["invoice_id"] == a.id
        # «Riprova» su una riga che chiede la conferma: senza, 409 con la spiegazione; con, parte.
        row_id = stuck["CONFLITTO/1"]["id"]
        created = self._spy(monkeypatch)
        refused = test_client.post(f"/api/reminders/followup/{row_id}/retry", json={"expected_mode": "reale"})
        assert refused.status_code == 409 and "Invia comunque" in refused.json()["detail"] and created == []
        forced = test_client.post(f"/api/reminders/followup/{row_id}/retry", json={"expected_mode": "reale", "force": True})
        assert forced.status_code == 200 and forced.json()["sent"] == 1
        assert created[0].sent[0]["to"] == ["amministrazione@cliente3.it"]
        after = test_client.get("/api/reminders/followup").json()
        assert [r["invoice_number"] for r in after["stuck"]] == ["FERMA/1"]
        # Un fallimento «certo» non chiede l'operatore (il giro lo riprova da solo).
        row = test_db_session.query(InvoiceFollowupEmail).filter_by(invoice_id=a.id).one()
        for kind, manual in (("validation", False), ("pdf", False), ("not_attempted", False), ("network", True),
                             ("provider", True), ("other", True)):
            row.error_kind = kind
            test_db_session.commit()
            item = test_client.get(f"/api/reminders/followup/{row.id}").json()
            assert item["needs_manual_retry"] is manual, kind
        # Fuori dai giorni di recupero il giro non la riprende più: anche un rifiuto certo tocca all'operatore.
        row.error_kind = "validation"
        a.due_date = date(2026, 10, 20)
        test_db_session.commit()
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert (item["needs_manual_retry"], item["will_auto_retry"]) == (True, False)
        a.due_date = DUE7
        test_db_session.commit()
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert (item["needs_manual_retry"], item["will_auto_retry"]) == (False, True)
        assert item["current_to"] == "amministrazione@shabu.it"
        # Una riga di TEST vista mentre si invia in reale: è un invio nuovo, lo fa il giro, nessuna conferma.
        row.error_kind, row.mode = "conflict", "test"
        test_db_session.commit()
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert (item["needs_manual_retry"], item["retry_needs_confirmation"], item["will_auto_retry"]) == (False, False, True)
        row.mode = "reale"
        test_db_session.commit()
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert (item["needs_manual_retry"], item["retry_needs_confirmation"], item["will_auto_retry"]) == (True, True, False)
        # Mentre si invia in TEST una riga incerta REALE resta dell'operatore (il giro non la tocca).
        row.error_kind = "network"
        test_db_session.commit()
        monkeypatch.setattr("backend.config.config.REMINDER_EMAIL_TEST_TO", "lorenzo@example.com")
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert (item["needs_manual_retry"], item["will_auto_retry"]) == (True, False)
        # Lo stesso per una riga incerta di TEST vista in test: lì non è «un invio nuovo».
        row.mode = "test"
        test_db_session.commit()
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert (item["needs_manual_retry"], item["will_auto_retry"]) == (True, False)
        row.mode = "reale"
        test_db_session.commit()
        monkeypatch.setattr("backend.config.config.REMINDER_EMAIL_TEST_TO", "")
        # Nel weekend vale la finestra di lunedì: un rifiuto certo è ancora «del giro».
        row.error_kind = "validation"
        test_db_session.commit()
        monkeypatch.setattr("backend.engine.followup_email.italian_today", lambda *a, **k: date(2026, 11, 21))
        monkeypatch.setattr("backend.engine.followup_email.followup_window",
                            lambda now=None, cfg=None: None if now is None else (date(2026, 11, 10), date(2026, 11, 16)))
        item = test_client.get(f"/api/reminders/followup/{row.id}").json()
        assert (item["needs_manual_retry"], item["will_auto_retry"]) == (False, True)
        monkeypatch.undo()
        for target in ("format", "reminder_email", "followup_email"):
            monkeypatch.setattr(f"backend.engine.{target}.datetime", _FixedDatetime)
        self._enable(monkeypatch)
        # L'indirizzo mostrato è quello del cliente a cui la fattura appartiene ORA.
        newowner = _other(test_db_session, 77)
        a.customer_id = newowner.id
        test_db_session.commit()
        assert test_client.get(f"/api/reminders/followup/{row.id}").json()["current_to"] == "amministrazione@cliente77.it"
        a.customer_id = c.id
        row.error_kind, row.mode = "conflict", "reale"
        test_db_session.commit()
        # Il limite dell'elenco vale sulle sole righe ancora da gestire.
        for n in range(8):
            gone = _due(test_db_session, _other(test_db_session, 300 + n), num=f"SALDATA/{n}")
            test_db_session.add(InvoiceFollowupEmail(invoice_id=gone.id, customer_id=gone.customer_id, due_date=DUE7,
                                                     status="failed", mode="reale", error_kind="network"))
            gone.status, gone.amount_due = "paid", 0.0
        test_db_session.commit()
        assert [r.id for r, _i, _c in fu.stuck_rows(test_db_session, limit=1)] == [row.id]
        # Le righe del promemoria non hanno il concetto di conferma.
        assert test_client.get("/api/reminders/email").json()["recent"][0]["retry_needs_confirmation"] is False

    def test_attachment_is_resolved_by_its_stored_name(self, test_client, test_db_session, monkeypatch):
        # L'allegato n-esimo è quello REGISTRATO all'invio, cercato per nome:
        # se l'ordine delle fatture cambia non arriva un altro documento.
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        a = _due(test_db_session, c, num="A/1", amount=100.0)
        _due(test_db_session, c, num="B/2", amount=200.0, due=DUE6)
        _sync(test_db_session)
        self._spy(monkeypatch)
        assert test_client.post("/api/reminders/followup/run").json()["sent"] == 1
        row = _rows(test_db_session)[0]
        assert row.attachments_json == ["copia_cortesia_fattura_A_1.pdf", "copia_cortesia_fattura_B_2.pdf"]
        a.due_date = date(2026, 11, 13)  # ora A viene DOPO B nell'ordine dell'email
        test_db_session.commit()
        first = test_client.get(f"/api/reminders/followup/{row.id}/attachments/0")
        assert first.status_code == 200 and "copia_cortesia_fattura_A_1.pdf" in first.headers["content-disposition"]
        assert "A/1" in _pdf_strings(first.content) and "B/2" not in _pdf_strings(first.content)
        second = test_client.get(f"/api/reminders/followup/{row.id}/attachments/1")
        assert "copia_cortesia_fattura_B_2.pdf" in second.headers["content-disposition"]
        assert test_client.get(f"/api/reminders/followup/{row.id}/attachments/2").status_code == 404
        assert test_client.get(f"/api/reminders/followup/{row.id}/attachments/-1").status_code == 404
        # Un nome registrato che oggi non si rigenera più: 404, non un altro file.
        row.attachments_json = ["copia_cortesia_fattura_SPARITA.pdf", "copia_cortesia_fattura_B_2.pdf"]
        test_db_session.commit()
        assert test_client.get(f"/api/reminders/followup/{row.id}/attachments/0").status_code == 404
        assert test_client.get(f"/api/reminders/followup/{row.id}/attachments/1").status_code == 200

    def test_customer_log_survives_a_broken_followup_registry(self, test_client, test_db_session, monkeypatch):
        # Lo storico dei promemoria (in produzione) non deve sparire se il
        # registro dei secondi avvisi non si legge.
        self._enable(monkeypatch)
        c = _cust(test_db_session)
        _due(test_db_session, c)
        monkeypatch.setattr("backend.engine.followup_email.group_members",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tabella assente")))
        log = test_client.get("/api/reminders/email/log", params={"customer_id": c.id})
        assert log.status_code == 200
        assert [(i["kind"], i["status"]) for i in log.json()["items"]] == [("promemoria", "sent")]

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
        assert weekend["merge_until"] is None
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

    def test_morning_jobs_wait_for_a_manual_run_instead_of_skipping(self, monkeypatch):
        # Un giro manuale (pulsante, «Riprova») tiene lo stesso lock: il giro
        # del mattino aspetta e riprova, non salta le email del giorno.
        from backend import scheduler as sched
        waits = []
        monkeypatch.setattr(sched.time, "sleep", lambda seconds: waits.append(seconds))
        answers = {"reminder": [{"skipped": "busy"}, {"skipped": "busy"}, {"sent": 3, "items": []}],
                   "followup": [{"sent": 1, "items": []}]}
        monkeypatch.setattr(sched, "run_reminder_email_job", lambda: answers["reminder"].pop(0))
        monkeypatch.setattr(sched, "run_followup_email_job", lambda: answers["followup"].pop(0))
        out = sched.run_email_jobs()
        assert out == {"reminder": {"sent": 3, "items": []}, "followup": {"sent": 1, "items": []}}
        assert waits == [sched.EMAIL_JOB_BUSY_WAIT_SECONDS] * 2
        # Occupato a oltranza: dopo i tentativi previsti si arrende e lo dice.
        waits.clear()
        calls = []
        monkeypatch.setattr(sched, "run_reminder_email_job", lambda: calls.append(1) or {"skipped": "busy"})
        monkeypatch.setattr(sched, "run_followup_email_job", lambda: {"blocked": "off"})
        out = sched.run_email_jobs()
        assert out["reminder"] == {"skipped": "busy"} and out["followup"] == {"blocked": "off"}
        assert len(calls) == sched.EMAIL_JOB_BUSY_RETRIES + 1 and len(waits) == sched.EMAIL_JOB_BUSY_RETRIES
        # Un errore o un esito qualunque non si riprova.
        waits.clear()
        monkeypatch.setattr(sched, "run_reminder_email_job", lambda: {"error": "db giù"})
        assert sched.run_email_jobs()["reminder"] == {"error": "db giù"} and waits == []

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
    def test_same_columns_as_the_reminder_registry_plus_the_key_age(self):
        # Il registro del secondo avviso ha le colonne di quello dei promemoria
        # (stessa lettura nel pannello e nello storico, comprese le chiavi
        # dell'email unica: `group_key` e `idempotency_key`) più il primo uso
        # della chiave.
        cols = lambda model: {c.name: (str(c.type), c.nullable) for c in model.__table__.columns}  # noqa: E731
        followup_cols = cols(InvoiceFollowupEmail)
        assert followup_cols.pop("key_first_used_at") == ("DATETIME", True)
        assert followup_cols == cols(InvoiceReminderEmail)
        assert followup_cols["group_key"] == ("VARCHAR", True) and followup_cols["idempotency_key"] == ("VARCHAR", True)
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
