"""Test dell'automazione oraria + progresso live del sync (batch 2026-07-17).

Copertura:
- _full_sync_task(include_order_matching=False) salta l'aggancio ordini
  Shopify ma esegue tutti gli altri passi
- _full_sync_task(include_order_matching=True) esegue anche l'aggancio ordini
- il tracker _sync_progress viene popolato durante il run e azzerato
  (running=False) alla fine, anche se un passo esplode
- _set_progress / _clear_progress unitari
- GET /sync/status espone il campo 'progress'
- run_hourly_job → _full_sync_task(include_order_matching=False)
- run_daily_job → _full_sync_task(include_order_matching=True)
"""

import pytest

from backend.api import sync as sync_mod


# ── Harness: sostituisce i singoli step con recorder veloci ──────────

def _patch_steps(monkeypatch, calls, snapshots=None):
    """Sostituisce ogni step-task del full sync con un finto veloce che
    registra la chiamata (e, opzionalmente, uno snapshot del progresso).

    invoices ritorna un esito che tiene lo step 'repair' DISATTIVO
    (anagrafica_ok=False → enrichment incompleto) così il test non tocca il
    motore di repair né il DB.
    """
    def rec(name, ret=None):
        def _fn(*args, **kwargs):
            calls.append(name)
            if snapshots is not None:
                snapshots.append((name, dict(sync_mod._sync_progress)))
            return ret if ret is not None else {"ok": True}
        return _fn

    monkeypatch.setattr(sync_mod, "_sync_invoices_task", rec(
        "invoices",
        {"fatturapro": {"success": True, "partial": False, "anagrafica_ok": False}},
    ))
    monkeypatch.setattr(sync_mod, "_sync_customers_task", rec("customers"))
    monkeypatch.setattr(sync_mod, "_run_matching_task", rec("matching"))
    monkeypatch.setattr(sync_mod, "_auto_create_task", rec("auto_create"))
    monkeypatch.setattr(sync_mod, "_case_lifecycle_task", rec("cases"))
    monkeypatch.setattr(sync_mod, "_match_orders_task", rec("order_matching"))


# ── include_order_matching ───────────────────────────────────────────

class TestIncludeOrderMatching:
    def test_light_run_skips_order_matching(self, monkeypatch):
        calls = []
        _patch_steps(monkeypatch, calls)

        results = sync_mod._full_sync_task(include_order_matching=False)

        # L'aggancio ordini NON è stato eseguito né compare nei risultati
        assert "order_matching" not in calls
        assert "order_matching" not in results
        # Ma tutti gli altri passi sì
        for step in ("invoices", "customers", "matching", "auto_create", "cases"):
            assert step in calls
            assert step in results

    def test_full_run_includes_order_matching(self, monkeypatch):
        calls = []
        _patch_steps(monkeypatch, calls)

        results = sync_mod._full_sync_task(include_order_matching=True)

        assert "order_matching" in calls
        assert "order_matching" in results
        # L'aggancio ordini è l'ULTIMO passo
        assert calls[-1] == "order_matching"

    def test_default_is_full(self, monkeypatch):
        calls = []
        _patch_steps(monkeypatch, calls)
        sync_mod._full_sync_task()
        assert "order_matching" in calls


# ── Progresso live ───────────────────────────────────────────────────

class TestSyncProgress:
    def test_progress_populated_during_run_and_cleared_after(self, monkeypatch):
        calls = []
        snapshots = []
        _patch_steps(monkeypatch, calls, snapshots)

        sync_mod._full_sync_task(include_order_matching=False, manual=True)

        # Durante il run il tracker era running=True con etichette italiane
        by_step = {name: prog for name, prog in snapshots}
        assert by_step["invoices"]["running"] is True
        assert by_step["invoices"]["step_index"] == 1
        assert by_step["invoices"]["step_label"] == "Fatture (FatturaPro)"
        assert by_step["invoices"]["total_steps"] == 6
        assert by_step["invoices"]["manual"] is True
        assert by_step["invoices"]["include_order_matching"] is False
        assert by_step["cases"]["step_index"] == 6
        assert by_step["cases"]["step_label"] == "Pratiche di recupero"

        # A fine run il progresso è chiuso
        assert sync_mod._sync_progress["running"] is False

    def test_total_steps_seven_when_full(self, monkeypatch):
        calls = []
        snapshots = []
        _patch_steps(monkeypatch, calls, snapshots)
        sync_mod._full_sync_task(include_order_matching=True)
        by_step = {name: prog for name, prog in snapshots}
        assert by_step["invoices"]["total_steps"] == 7
        assert by_step["order_matching"]["step_index"] == 7
        assert by_step["order_matching"]["step_label"] == "Aggancio ordini Shopify"

    def test_running_false_even_if_a_step_explodes(self, monkeypatch):
        calls = []
        _patch_steps(monkeypatch, calls)

        def boom():
            raise RuntimeError("boom")

        # Il matching esplode: il full sync cattura per-step, ma anche se
        # un'eccezione arrivasse fino al finally, running deve tornare False.
        monkeypatch.setattr(sync_mod, "_run_matching_task", boom)
        sync_mod._full_sync_task(include_order_matching=False)
        assert sync_mod._sync_progress["running"] is False

    def test_set_and_clear_progress_units(self):
        sync_mod._set_progress("matching", "Abbinamento fatture", 4, 7)
        assert sync_mod._sync_progress["step_key"] == "matching"
        assert sync_mod._sync_progress["step_label"] == "Abbinamento fatture"
        assert sync_mod._sync_progress["step_index"] == 4
        assert sync_mod._sync_progress["total_steps"] == 7
        assert sync_mod._sync_progress["updated_at"] is not None

        sync_mod._sync_progress["running"] = True
        sync_mod._clear_progress()
        assert sync_mod._sync_progress["running"] is False
        assert sync_mod._sync_progress["step_key"] is None
        assert sync_mod._sync_progress["step_label"] is None

    def test_status_endpoint_exposes_progress(self, test_client):
        resp = test_client.get("/api/sync/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "progress" in body
        progress = body["progress"]
        for key in (
            "running", "step_key", "step_label", "step_index",
            "total_steps", "manual", "include_order_matching",
        ):
            assert key in progress


# ── Scheduler: job orario vs giornaliero ─────────────────────────────

class TestSchedulerJobs:
    def test_hourly_job_runs_light_sync(self, monkeypatch):
        captured = {}

        def fake_full(include_order_matching=True, manual=False):
            captured["include_order_matching"] = include_order_matching
            captured["manual"] = manual
            return {"ok": True}

        monkeypatch.setattr(sync_mod, "_full_sync_task", fake_full)
        from backend.scheduler import run_hourly_job
        run_hourly_job()
        assert captured["include_order_matching"] is False

    def test_hourly_skips_when_colliding_with_daily_slot(self, monkeypatch):
        # Se il daily è al minuto 0 (stesso istante dell'orario) e siamo in
        # quell'ora, l'orario cede la precedenza al full: NON deve girare
        # (altrimenti il _sync_lock potrebbe far scartare il giornaliero).
        import pytz
        from datetime import datetime
        from backend.config import config
        from backend.scheduler import run_hourly_job

        def boom(*a, **kw):
            raise AssertionError("l'orario NON doveva girare in collisione col daily")

        monkeypatch.setattr(sync_mod, "_full_sync_task", boom)
        now_hour = datetime.now(pytz.timezone(config.TIMEZONE)).hour
        monkeypatch.setattr(config, "SCHEDULER_MINUTE", 0)
        monkeypatch.setattr(config, "SCHEDULER_HOUR", now_hour)
        res = run_hourly_job()
        assert "skipped" in res

    def test_daily_job_runs_full_sync(self, monkeypatch):
        captured = {}

        def fake_full(include_order_matching=True, manual=False):
            captured["include_order_matching"] = include_order_matching
            captured["manual"] = manual
            return {"ok": True}

        monkeypatch.setattr(sync_mod, "_full_sync_task", fake_full)
        from backend.scheduler import run_daily_job
        run_daily_job(manual=True)
        assert captured["include_order_matching"] is True
        assert captured["manual"] is True

    def test_scheduler_status_reports_hourly(self):
        from backend.scheduler import get_scheduler_status
        status = get_scheduler_status()
        assert status["hourly_enabled"] is True
        assert "next_run_times" in status
        # I campi storici restano presenti (firma non rotta)
        for key in ("running", "scheduler_hour", "scheduler_minute", "timezone", "last_sync"):
            assert key in status
