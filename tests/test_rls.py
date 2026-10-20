"""RLS all'avvio: ogni tabella mappata dai modelli deve essere coperta.

Regressione dell'alert Supabase «CRITICAL: Table publicly accessible —
rls_disabled_in_public» (21/10/2026): recovery_action_invoices era creata
da create_all ma mancava dalla lista scritta a mano di _enable_rls.
"""
from unittest.mock import MagicMock

from backend import database
from backend.database import Base, _enable_rls, _rls_tables


def _fake_engine():
    raw = MagicMock()
    cur = MagicMock()
    raw.cursor.return_value = cur
    engine = MagicMock()
    engine.raw_connection.return_value = raw
    return engine, raw, cur


def _executed(cur):
    return [c.args[0] for c in cur.execute.call_args_list]


def test_rls_tables_cover_every_mapped_table():
    mapped = {t.name for t in Base.metadata.sorted_tables}
    assert mapped <= set(_rls_tables())
    # la tabella che ha fatto scattare l'alert
    assert "recovery_action_invoices" in _rls_tables()


def test_rls_tables_keep_legacy_and_no_duplicates():
    tables = _rls_tables()
    assert {"messages", "conversations"} <= set(tables)
    assert len(tables) == len(set(tables))


def test_enable_rls_runs_for_every_table_and_revokes_data_api_roles(monkeypatch):
    monkeypatch.setattr(database.config, "DATABASE_URL", "postgresql://u:p@h/db")
    engine, raw, cur = _fake_engine()
    _enable_rls(engine)
    sql = _executed(cur)
    for t in _rls_tables():
        assert f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY" in sql
        assert (
            f"CREATE POLICY backend_full_access ON {t} "
            f"FOR ALL TO postgres USING (true) WITH CHECK (true)"
        ) in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated" in sql
    assert any(s.startswith("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES") for s in sql)
    assert raw.commit.call_count == len(sql)
    raw.close.assert_called_once()


def test_enable_rls_one_failure_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(database.config, "DATABASE_URL", "postgresql://u:p@h/db")
    engine, raw, cur = _fake_engine()
    first_table = _rls_tables()[0]

    def _exec(stmt):
        if stmt == f"ALTER TABLE {first_table} ENABLE ROW LEVEL SECURITY":
            raise RuntimeError("boom")

    cur.execute.side_effect = _exec
    _enable_rls(engine)
    sql = _executed(cur)
    assert raw.rollback.call_count == 1
    for t in _rls_tables()[1:]:
        assert f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated" in sql


def test_enable_rls_skips_sqlite(monkeypatch):
    monkeypatch.setattr(database.config, "DATABASE_URL", "sqlite:///:memory:")
    engine, raw, cur = _fake_engine()
    _enable_rls(engine)
    engine.raw_connection.assert_not_called()
