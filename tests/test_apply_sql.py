"""schema_migrations tracking in scripts.apply_sql."""
from __future__ import annotations

import sys
import types

from scripts import apply_sql


class _Cursor:
    def __init__(self, applied: set[str]):
        self.applied = applied
        self.statements: list[str] = []
        self.params: list = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(params)

    def fetchall(self):
        return [(name,) for name in self.applied]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Conn:
    def __init__(self, applied: set[str]):
        self.cur = _Cursor(applied)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _install(monkeypatch, conn):
    module = types.SimpleNamespace(connect=lambda url: conn)
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: types.SimpleNamespace(database_url="postgresql://test"),
    )


def test_already_applied_files_are_skipped(monkeypatch, tmp_path):
    conn = _Conn(applied={"01_schema.sql"})
    _install(monkeypatch, conn)

    failures = apply_sql.apply_migrations(
        ["01_schema.sql", "02_indexes.sql"], log=lambda *_: None
    )

    assert failures == []
    bodies = [s for s in conn.cur.statements if "CREATE TABLE IF NOT EXISTS schema_migrations" not in s]
    assert not any("CREATE TYPE" in s for s in bodies), "01 must not run again"
    assert any("ON CONFLICT (filename)" in s for s in bodies)


def test_force_reruns_recorded_files(monkeypatch):
    conn = _Conn(applied={"01_schema.sql"})
    _install(monkeypatch, conn)

    apply_sql.apply_migrations(["01_schema.sql"], log=lambda *_: None, force=True)

    assert any("CREATE TYPE" in s for s in conn.cur.statements)


def test_order_includes_atomic_stats_migration():
    assert "19_atomic_stats.sql" in apply_sql.ORDER
