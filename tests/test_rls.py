"""RLS all'avvio: ogni tabella mappata dai modelli deve essere coperta.

Regressione dell'alert Supabase «CRITICAL: Table publicly accessible —
rls_disabled_in_public» (21/10/2026): recovery_action_invoices era creata
da create_all ma mancava dalla lista scritta a mano di _enable_rls.
"""
import logging
from unittest.mock import MagicMock

from backend import database
from backend.database import (
    Base, _LEGACY_TABLES, _RLS_STATE_SQL, _enable_rls, _rls_qualified,
    _rls_statements, _rls_tables,
)

MAPPED = {t.name for t in Base.metadata.sorted_tables}


def _fake_engine(state_rows=()):
    raw = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = list(state_rows)
    raw.cursor.return_value = cur
    engine = MagicMock()
    engine.raw_connection.return_value = raw
    return engine, raw, cur


def _executed(cur):
    return [c.args[0] for c in cur.execute.call_args_list]


def _ddl(cur):
    """Solo gli statement di protezione (senza lock_timeout e lettura stato)."""
    return [s for s in _executed(cur) if s != _RLS_STATE_SQL and not s.startswith("SET ")]


def test_rls_tables_cover_every_mapped_table():
    assert MAPPED <= set(_rls_tables())
    # la tabella che ha fatto scattare l'alert
    assert "recovery_action_invoices" in _rls_tables()


def test_rls_tables_keep_legacy_and_no_duplicates():
    tables = _rls_tables()
    assert set(_LEGACY_TABLES) <= set(tables)
    assert len(tables) == len(set(tables))


def test_qualified_names_are_quoted():
    assert _rls_qualified("order") == 'public."order"'
    assert _rls_qualified('a"b') == 'public."a""b"'


def test_statements_cover_mapped_tables_with_policy_and_legacy_without():
    sql = _rls_statements()
    for t in MAPPED:
        q = _rls_qualified(t)
        assert f"ALTER TABLE {q} ENABLE ROW LEVEL SECURITY" in sql
        assert (
            f"CREATE POLICY backend_full_access ON {q} "
            f"FOR ALL TO postgres USING (true) WITH CHECK (true)"
        ) in sql
    for t in _LEGACY_TABLES:
        q = _rls_qualified(t)
        assert f"ALTER TABLE IF EXISTS {q} ENABLE ROW LEVEL SECURITY" in sql
        assert not any(s.endswith(f"ON {q}") and "POLICY" in s for s in sql)
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated" in sql
    assert "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated" in sql
    assert any(s.startswith("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES") for s in sql)


def test_statements_skip_tables_already_compliant():
    sql = _rls_statements(rls_on={"customers"}, policy_on={"customers"})
    q = _rls_qualified("customers")
    assert not any(s.startswith(f"ALTER TABLE {q}") for s in sql)
    assert not any(s.endswith(f"ON {q}") for s in sql)
    # le altre restano
    assert f"ALTER TABLE {_rls_qualified('invoices')} ENABLE ROW LEVEL SECURITY" in sql
    # a regime (tutto già a posto) restano solo le revoche a livello di schema
    steady = _rls_statements(rls_on=set(_rls_tables()), policy_on=MAPPED)
    assert all(s.startswith(("REVOKE", "ALTER DEFAULT PRIVILEGES")) for s in steady)


def test_enable_rls_reads_state_then_runs_every_statement(monkeypatch):
    monkeypatch.setattr(database.config, "DATABASE_URL", "postgresql://u:p@h/db")
    engine, raw, cur = _fake_engine()
    _enable_rls(engine)
    assert _executed(cur)[0] == "SET lock_timeout = '10s'"
    assert _RLS_STATE_SQL in _executed(cur)
    assert _ddl(cur) == _rls_statements()
    # un commit per statement di protezione (+1 per il SET)
    assert raw.commit.call_count == len(_ddl(cur)) + 1
    raw.close.assert_called_once()


def test_enable_rls_uses_db_state_to_skip_done_tables(monkeypatch):
    monkeypatch.setattr(database.config, "DATABASE_URL", "postgresql://u:p@h/db")
    engine, raw, cur = _fake_engine(state_rows=[("customers", True, True), ("invoices", True, False)])
    _enable_rls(engine)
    ddl = _ddl(cur)
    qc, qi = _rls_qualified("customers"), _rls_qualified("invoices")
    assert not any(qc in s for s in ddl)
    assert f"ALTER TABLE {qi} ENABLE ROW LEVEL SECURITY" not in ddl
    assert f"DROP POLICY IF EXISTS backend_full_access ON {qi}" in ddl


def test_enable_rls_one_failure_is_logged_and_does_not_stop_the_others(monkeypatch, caplog):
    monkeypatch.setattr(database.config, "DATABASE_URL", "postgresql://u:p@h/db")
    engine, raw, cur = _fake_engine()
    bad = f"ALTER TABLE {_rls_qualified('customers')} ENABLE ROW LEVEL SECURITY"

    def _exec(stmt):
        if stmt == bad:
            raise RuntimeError("must be owner of table customers")

    cur.execute.side_effect = _exec
    with caplog.at_level(logging.WARNING, logger="backend.database"):
        _enable_rls(engine)
    ddl = _ddl(cur)
    assert bad in ddl
    assert ddl == _rls_statements()  # tutti eseguiti, anche dopo il fallimento
    msgs = [r.getMessage() for r in caplog.records]
    assert any("RLS statement failed" in m and "must be owner" in m for m in msgs)
    assert any(r.levelno == logging.ERROR and "INCOMPLETE" in r.getMessage() for r in caplog.records)
    assert not any("u:p@h" in m for m in msgs)


def test_enable_rls_state_query_failure_falls_back_to_full_run(monkeypatch):
    monkeypatch.setattr(database.config, "DATABASE_URL", "postgresql://u:p@h/db")
    engine, raw, cur = _fake_engine()

    def _exec(stmt):
        if stmt == _RLS_STATE_SQL:
            raise RuntimeError("permission denied")

    cur.execute.side_effect = _exec
    _enable_rls(engine)
    assert _ddl(cur) == _rls_statements()


def test_enable_rls_skips_sqlite(monkeypatch):
    monkeypatch.setattr(database.config, "DATABASE_URL", "sqlite:///:memory:")
    engine, raw, cur = _fake_engine()
    _enable_rls(engine)
    engine.raw_connection.assert_not_called()
