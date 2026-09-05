from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Iterator

from .models import OrderDraft, TradeBatch, TradeSnapshot, TradeStatus


@contextmanager
def _connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _local_day_utc_bounds(day: date | None = None) -> tuple[str, str]:
    local_day = day or date.today()
    local_start = datetime.combine(local_day, time.min).astimezone()
    local_end = datetime.combine(local_day + timedelta(days=1), time.min).astimezone()
    return _dt(local_start), _dt(local_end)


class Repository:
    def __init__(self, sqlite_path: str | Path) -> None:
        self.sqlite_path = Path(sqlite_path)
        self._initialized = False
        self._init_lock = Lock()

    def init_db(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with _connect(self.sqlite_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS trade_batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        expiration TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        put_symbol TEXT NOT NULL,
                        put_strike REAL NOT NULL,
                        call_symbol TEXT NOT NULL,
                        call_strike REAL NOT NULL,
                        original_credit REAL NOT NULL,
                        opened_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        notes TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS trade_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        underlying_price REAL,
                        close_debit_mid REAL NOT NULL,
                        close_debit_conservative REAL NOT NULL,
                        pnl_mid REAL NOT NULL,
                        profit_pct REAL NOT NULL,
                        dte INTEGER NOT NULL,
                        alert_state TEXT NOT NULL,
                        FOREIGN KEY(trade_id) REFERENCES trade_batches(id)
                    );

                    CREATE TABLE IF NOT EXISTS order_drafts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id INTEGER,
                        created_at TEXT NOT NULL,
                        action TEXT NOT NULL,
                        order_json TEXT NOT NULL,
                        estimated_price REAL NOT NULL,
                        status TEXT NOT NULL,
                        broker_order_id TEXT,
                        broker_status TEXT,
                        FOREIGN KEY(trade_id) REFERENCES trade_batches(id)
                    );
                    """
                )
                self._ensure_order_drafts_columns(conn)
            self._initialized = True

    @staticmethod
    def _ensure_order_drafts_columns(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(order_drafts)").fetchall()}
        if "broker_order_id" not in columns:
            conn.execute("ALTER TABLE order_drafts ADD COLUMN broker_order_id TEXT")
        if "broker_status" not in columns:
            conn.execute("ALTER TABLE order_drafts ADD COLUMN broker_status TEXT")
        if "replaces_order_id" not in columns:
            conn.execute("ALTER TABLE order_drafts ADD COLUMN replaces_order_id TEXT")

    def add_trade_batch(self, trade: TradeBatch) -> int:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO trade_batches (
                    symbol, expiration, quantity, put_symbol, put_strike,
                    call_symbol, call_strike, original_credit, opened_at, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.symbol.upper(),
                    trade.expiration.isoformat(),
                    trade.quantity,
                    trade.put_symbol,
                    trade.put_strike,
                    trade.call_symbol,
                    trade.call_strike,
                    trade.original_credit,
                    _dt(trade.opened_at),
                    trade.status.value if hasattr(trade.status, "value") else trade.status,
                    trade.notes,
                ),
            )
            return int(cursor.lastrowid)

    def delete_trade_batch(self, trade_id: int) -> None:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            conn.execute("DELETE FROM trade_batches WHERE id = ?", (trade_id,))

    def get_trade_batch(self, trade_id: int) -> TradeBatch:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            row = conn.execute(
                "SELECT * FROM trade_batches WHERE id = ?",
                (trade_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Trade batch not found: {trade_id}")
        return self._row_to_trade(row)

    def list_trade_batches(self, *, statuses: Iterable[TradeStatus | str] | None = None) -> list[TradeBatch]:
        self.init_db()
        params: list[Any] = []
        sql = "SELECT * FROM trade_batches"
        if statuses:
            normalized = [status.value if hasattr(status, "value") else str(status) for status in statuses]
            placeholders = ", ".join("?" for _ in normalized)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(normalized)
        sql += " ORDER BY opened_at DESC, id DESC"
        with _connect(self.sqlite_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_trade(row) for row in rows]

    def add_snapshot(self, snapshot: TradeSnapshot) -> int:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO trade_snapshots (
                    trade_id, timestamp, underlying_price, close_debit_mid,
                    close_debit_conservative, pnl_mid, profit_pct, dte, alert_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.trade_id,
                    _dt(snapshot.timestamp),
                    snapshot.underlying_price,
                    snapshot.close_debit_mid,
                    snapshot.close_debit_conservative,
                    snapshot.pnl_mid,
                    snapshot.profit_pct,
                    snapshot.dte,
                    snapshot.alert_state.value,
                ),
            )
            return int(cursor.lastrowid)

    def add_order_draft(self, draft: OrderDraft) -> int:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO order_drafts (
                    trade_id, created_at, action, order_json, estimated_price, status,
                    broker_order_id, broker_status, replaces_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.trade_id,
                    _dt(draft.created_at),
                    draft.action,
                    json.dumps(draft.order_json, sort_keys=True),
                    draft.estimated_price,
                    draft.status,
                    draft.broker_order_id,
                    draft.broker_status,
                    draft.replaces_order_id,
                ),
            )
            return int(cursor.lastrowid)

    def list_order_drafts(self, *, limit: int | None = 100, only_today: bool = False) -> list[OrderDraft]:
        self.init_db()
        params: list[Any] = []
        sql = "SELECT * FROM order_drafts"
        if only_today:
            start, end = _local_day_utc_bounds()
            sql += " WHERE created_at >= ? AND created_at < ?"
            params.extend((start, end))
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with _connect(self.sqlite_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_order_draft(row) for row in rows]

    def update_order_status(self, draft_id: int, status: str) -> None:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            conn.execute(
                "UPDATE order_drafts SET status = ? WHERE id = ?",
                (status, draft_id),
            )

    def update_order_broker_status(
        self,
        draft_id: int,
        *,
        broker_order_id: str | None = None,
        broker_status: str | None = None,
    ) -> None:
        self.init_db()
        assignments: list[str] = []
        params: list[Any] = []
        if broker_order_id is not None:
            assignments.append("broker_order_id = ?")
            params.append(broker_order_id)
        if broker_status is not None:
            assignments.append("broker_status = ?")
            params.append(broker_status)
        if not assignments:
            return
        params.append(draft_id)
        with _connect(self.sqlite_path) as conn:
            conn.execute(
                f"UPDATE order_drafts SET {', '.join(assignments)} WHERE id = ?",
                params,
            )

    def count_open_batches(self) -> int:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM trade_batches WHERE status = ?",
                (TradeStatus.OPEN.value,),
            ).fetchone()
        return int(row["count"])

    def count_new_batches_today(self) -> int:
        self.init_db()
        start, end = _local_day_utc_bounds()
        with _connect(self.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM trade_batches
                WHERE opened_at >= ? AND opened_at < ? AND status IN (?, ?)
                """,
                (start, end, TradeStatus.OPEN.value, TradeStatus.PENDING_OPEN.value),
            ).fetchone()
        return int(row["count"])

    def total_open_stop_risk(self, stop_multiple: float) -> float:
        self.init_db()
        with _connect(self.sqlite_path) as conn:
            rows = conn.execute(
                """
                SELECT original_credit, quantity
                FROM trade_batches
                WHERE status = ?
                """,
                (TradeStatus.OPEN.value,),
            ).fetchall()
        return sum(max(0.0, float(row["original_credit"]) * (stop_multiple - 1) * 100 * int(row["quantity"])) for row in rows)

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> TradeBatch:
        return TradeBatch(
            id=int(row["id"]),
            symbol=row["symbol"],
            expiration=date.fromisoformat(row["expiration"]),
            quantity=int(row["quantity"]),
            put_symbol=row["put_symbol"],
            put_strike=float(row["put_strike"]),
            call_symbol=row["call_symbol"],
            call_strike=float(row["call_strike"]),
            original_credit=float(row["original_credit"]),
            opened_at=_parse_dt(row["opened_at"]),
            status=TradeStatus(row["status"]),
            notes=row["notes"] or "",
        )

    @staticmethod
    def _row_to_order_draft(row: sqlite3.Row) -> OrderDraft:
        return OrderDraft(
            id=int(row["id"]),
            trade_id=None if row["trade_id"] is None else int(row["trade_id"]),
            created_at=_parse_dt(row["created_at"]),
            action=row["action"],
            order_json=json.loads(row["order_json"]),
            estimated_price=float(row["estimated_price"]),
            status=row["status"],
            broker_order_id=row["broker_order_id"],
            broker_status=row["broker_status"],
            replaces_order_id=row["replaces_order_id"],
        )
