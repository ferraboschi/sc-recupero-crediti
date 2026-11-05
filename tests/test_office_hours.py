"""Regola owner (5/11/2026): FatturaPro una volta al giorno al mattino, mai
in orario d'ufficio (sessione unica: il nostro login sconnette il personale)."""
from datetime import datetime

import httpx
import pytz

from backend import config as cfgmod
from backend.api import sync as sync_mod
from backend.connectors.fatturapro import FatturaProConnector
from backend.engine import office_hours as oh

ROME = pytz.timezone("Europe/Rome")


def _rome(h, m=0):
    return ROME.localize(datetime(2026, 11, 5, h, m))


def test_window_parsing(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", "8-18")
    assert oh.quiet_hours_window() == (8, 18)
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", " 9 - 17 ")
    assert oh.quiet_hours_window() == (9, 17)
    for bad in ("", "abc", "8", "25-3", "8-8"):
        monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", bad)
        assert oh.quiet_hours_window() == (0, 0)
        assert oh.in_quiet_hours(_rome(12)) is False


def test_boundaries_rome_time(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", "8-18")
    assert oh.in_quiet_hours(_rome(7, 59)) is False
    assert oh.in_quiet_hours(_rome(8, 0)) is True
    assert oh.in_quiet_hours(_rome(12)) is True
    assert oh.in_quiet_hours(_rome(17, 59)) is True
    assert oh.in_quiet_hours(_rome(18, 0)) is False
    assert oh.in_quiet_hours(_rome(23)) is False
    # l'ora conta in Italia, non nel fuso del chiamante (Giappone = +8h d'inverno)
    tokyo = pytz.timezone("Asia/Tokyo").localize(datetime(2026, 11, 5, 18, 0))  # 10:00 a Roma
    assert oh.in_quiet_hours(tokyo) is True
    naive = datetime(2026, 11, 5, 7, 0)  # interpretato come ora di Roma
    assert oh.in_quiet_hours(naive) is False


def test_window_across_midnight(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", "22-6")
    assert oh.in_quiet_hours(_rome(23)) is True
    assert oh.in_quiet_hours(_rome(3)) is True
    assert oh.in_quiet_hours(_rome(6)) is False
    assert oh.in_quiet_hours(_rome(12)) is False


def test_message_names_hours_and_daily_time(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", "8-18")
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_SYNC_HOUR", 7)
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_SYNC_MINUTE", 0)
    msg = oh.quiet_hours_message()
    assert "08:00-18:00" in msg and "07:00" in msg and "dalle 18:00" in msg


def test_login_gate_makes_no_network_call(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_USERNAME", "u@example.com")
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_PASSWORD", "x")
    monkeypatch.setattr("backend.connectors.fatturapro.in_quiet_hours", lambda: True)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="<html></html>")

    conn = FatturaProConnector()
    conn.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert conn.login() is False
    assert conn.quiet_hours_block is True and conn.last_login_error is None
    assert calls == []
    # force=True (uso esplicito dell'owner) supera il gate: si tenta davvero
    assert conn.login(force=True) is False
    assert conn.quiet_hours_block is False and len(calls) > 0


def test_sync_error_message_in_quiet_hours(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", "8-18")

    class _Blocked:
        quiet_hours_block = True
        last_login_error = None

    assert sync_mod._login_failure_error(_Blocked()).startswith("FatturaPro non consultato: orario d'ufficio")


def test_skip_reason(monkeypatch):
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", "8-18")
    assert "una volta al giorno" in sync_mod._fatturapro_skip_reason(False, _rome(3))
    assert "orario d'ufficio" in sync_mod._fatturapro_skip_reason(True, _rome(10))
    assert sync_mod._fatturapro_skip_reason(True, _rome(7)) is None


def test_invoice_passes_merge_and_stop_on_partial(monkeypatch):
    seq = iter([
        {"fatturapro": {"success": True, "partial": False, "created": 2, "paid_detected": 0}},
        {"fatturapro": {"success": True, "partial": False, "created": 0, "paid_detected": 3}},
    ])
    monkeypatch.setattr(sync_mod, "_sync_invoices_task", lambda: next(seq))
    res = sync_mod._run_invoice_passes(2)
    assert res["fatturapro"]["created"] == 2 and res["fatturapro"]["paid_detected"] == 3
    assert res["fatturapro"]["passes"] == 2

    seq2 = iter([{"fatturapro": {"success": True, "partial": True, "created": 1}}, {"boom": 1}])
    monkeypatch.setattr(sync_mod, "_sync_invoices_task", lambda: next(seq2))
    res = sync_mod._run_invoice_passes(2)
    assert res["fatturapro"]["passes"] == 1 and res["fatturapro"]["partial"] is True


def test_full_sync_skips_invoices_without_touching_persisted_state(monkeypatch):
    """Sync orario (include_fatturapro=False): il passo fatture non parte e lo
    stato 'invoices' non viene riscritto; gli altri passi girano."""
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_QUIET_HOURS", "8-18")
    called = {"invoices": 0, "persist": []}
    monkeypatch.setattr(sync_mod, "_sync_invoices_task", lambda: called.__setitem__("invoices", called["invoices"] + 1) or {})
    monkeypatch.setattr(sync_mod, "_persist_sync_status", lambda key, result: called["persist"].append(key))
    for name in ("_sync_customers_task", "_run_matching_task", "_auto_create_task", "_match_orders_task"):
        monkeypatch.setattr(sync_mod, name, lambda *a, **k: {"success": True})
    monkeypatch.setattr(sync_mod, "_case_lifecycle_task", lambda allow_close=False: {"opened": 0})
    monkeypatch.setattr(sync_mod, "auto_merge_exact_piva", lambda s: {}, raising=False)
    res = sync_mod._full_sync_task(include_order_matching=False, include_fatturapro=False)
    assert called["invoices"] == 0
    assert "una volta al giorno" in res["invoices"]["skipped"]
    assert "invoices" not in called["persist"]
    assert "customers" in res and "cases" in res


def test_reconcile_refuses_in_quiet_hours(monkeypatch):
    monkeypatch.setattr(sync_mod, "in_quiet_hours", lambda now=None: True)
    monkeypatch.setattr(sync_mod, "_sync_invoices_task", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    res = sync_mod._reconcile_incassi_task()
    assert res["passes"] == 0 and res["marked_paid"] == 0
    assert "orario d'ufficio" in res["message"]


def test_verify_endpoint_423_in_quiet_hours(monkeypatch, test_client, test_db_session):
    from backend.api import customers as cust_mod
    from backend.database import Customer
    c = Customer(ragione_sociale="ACME SRL")
    test_db_session.add(c)
    test_db_session.commit()
    monkeypatch.setattr(cust_mod, "in_quiet_hours", lambda now=None: True)
    r = test_client.post(f"/api/customers/{c.id}/verify-fatturapro")
    assert r.status_code == 423
    assert "orario d'ufficio" in r.json()["detail"]
    r2 = test_client.post(f"/api/customers/{c.id}/verify-fatturapro/apply", json={"fixes": []})
    assert r2.status_code == 423


def test_scheduler_jobs_keep_fatturapro_out_of_hourly_and_daily(monkeypatch):
    from backend import scheduler as sched
    captured = []

    def fake_full(**kwargs):
        captured.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(sync_mod, "_full_sync_task", fake_full)
    sched.run_hourly_job()
    sched.run_daily_job(manual=True)
    sched.run_fatturapro_job()
    assert [k.get("include_fatturapro") for k in captured] == [False, False, True]
    assert captured[2]["fatturapro_passes"] == 2 and captured[2]["include_order_matching"] is False


def test_scheduler_registers_daily_fatturapro_job(monkeypatch):
    from backend import scheduler as sched
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_SYNC_HOUR", 7)
    monkeypatch.setattr(cfgmod.config, "FATTURAPRO_SYNC_MINUTE", 0)
    monkeypatch.setattr(sched, "_scheduler", None)
    monkeypatch.setattr(sched, "_scheduler_started", False)
    sched.start_scheduler()
    try:
        job = sched._scheduler.get_job("fatturapro_daily_job")
        assert job is not None
        nxt = job.next_run_time.astimezone(ROME)
        assert (nxt.hour, nxt.minute) == (7, 0)
        status = sched.get_scheduler_status()
        assert status["fatturapro_hour"] == 7 and status["fatturapro_quiet_hours"] == cfgmod.config.FATTURAPRO_QUIET_HOURS
        assert "fatturapro_daily_job" in status["next_run_times"]
    finally:
        sched.stop_scheduler()
