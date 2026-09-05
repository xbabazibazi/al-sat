"""Kalıcı durum deposu (SQLite).

Botun en kritik güvenlik katmanlarından biri: pozisyon durumu, izleyen stop
seviyesi ve işlenen son mum RAM'de DEĞİL diskte tutulur. Bot/konteyner
çökse ve `restart: always` ile yeniden başlasa bile:
  - açık pozisyon unutulmaz (çifte alım engellenir),
  - stop emri kimliği bilinir (borsayla mutabakat yapılabilir),
  - aynı mum ikinci kez işlenmez.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    highest_price: float
    trailing_stop: float
    stop_order_id: Optional[int]          # borsadaki STOP_LOSS_LIMIT emrinin id'si
    entry_time: str                        # ISO-8601 UTC
    entry_fee_usdt: float = 0.0


class StateStore:
    def __init__(self, db_path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    data   TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS last_candle (
                    symbol TEXT PRIMARY KEY,
                    open_time_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry_time TEXT, exit_time TEXT,
                    entry_price REAL, exit_price REAL,
                    qty REAL, pnl_usdt REAL, pnl_pct REAL,
                    exit_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    # ---------------------------------------------------------------- pozisyon
    def get_position(self, symbol: str) -> Optional[Position]:
        cur = self._conn.execute("SELECT data FROM positions WHERE symbol=?", (symbol,))
        row = cur.fetchone()
        return Position(**json.loads(row[0])) if row else None

    def save_position(self, pos: Position) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO positions(symbol, data) VALUES(?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET data=excluded.data",
                (pos.symbol, json.dumps(asdict(pos))),
            )

    def clear_position(self, symbol: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    # ------------------------------------------------------------- mum takibi
    def get_last_candle(self, symbol: str) -> int:
        cur = self._conn.execute("SELECT open_time_ms FROM last_candle WHERE symbol=?", (symbol,))
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def set_last_candle(self, symbol: str, open_time_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO last_candle(symbol, open_time_ms) VALUES(?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET open_time_ms=excluded.open_time_ms",
                (symbol, open_time_ms),
            )

    # ---------------------------------------------------------------- işlemler
    def record_trade(
        self, symbol: str, entry_time: str, exit_time: str,
        entry_price: float, exit_price: float, qty: float, exit_reason: str,
    ) -> tuple[float, float]:
        pnl_usdt = (exit_price - entry_price) * qty
        pnl_pct = (exit_price / entry_price - 1.0) * 100.0 if entry_price else 0.0
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trades(symbol, entry_time, exit_time, entry_price, exit_price,"
                " qty, pnl_usdt, pnl_pct, exit_reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (symbol, entry_time, exit_time, entry_price, exit_price, qty, pnl_usdt, pnl_pct, exit_reason),
            )
        return pnl_usdt, pnl_pct

    def todays_realized_pnl(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_usdt), 0) FROM trades WHERE exit_time >= ?", (today,)
        )
        return float(cur.fetchone()[0])

    def trade_stats(self) -> dict:
        cur = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl_usdt),0),"
            " COALESCE(SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END),0) FROM trades"
        )
        n, total, wins = cur.fetchone()
        return {"count": n, "total_pnl": total, "wins": wins}

    # ------------------------------------------------------------ anahtar/değer
    def get_kv(self, key: str, default: str = "") -> str:
        cur = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
