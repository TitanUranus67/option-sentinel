from __future__ import annotations

import sqlite3

import pytest

from option_sentinel.persistence import _connect


def test_connection_context_closes_database_handle(tmp_path) -> None:
    with _connect(tmp_path / "closed.db") as connection:
        connection.execute("CREATE TABLE sample (value INTEGER)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT * FROM sample")
