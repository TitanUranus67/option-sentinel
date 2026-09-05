from __future__ import annotations

import sqlite3

import pytest

from option_sentinel.persistence import Repository, _connect


def test_connection_context_closes_database_handle(tmp_path) -> None:
    with _connect(tmp_path / "closed.db") as connection:
        connection.execute("CREATE TABLE sample (value INTEGER)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT * FROM sample")


def test_initialization_preserves_historical_snapshot_data(tmp_path):
    path = tmp_path / "legacy.db"
    with _connect(path) as connection:
        connection.execute("CREATE TABLE trade_snapshots (id INTEGER PRIMARY KEY, trade_id INTEGER, pnl_mid REAL)")
        connection.execute("INSERT INTO trade_snapshots VALUES (1, 7, 12.50)")
    Repository(path).init_db()
    with _connect(path) as connection:
        row = connection.execute("SELECT * FROM trade_snapshots").fetchone()
    assert tuple(row) == (1, 7, 12.50)
