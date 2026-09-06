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
    highest_price: float                   # long: en yüksek, short: en düşük görülen fiyat (uç değer)
    trailing_stop: float
    stop_order_id: Optional[int]          # borsadaki STOP_LOSS_LIMIT emrinin id'si (paper modda None)
    entry_time: str                        # ISO-8601 UTC
    entry_fee_usdt: float = 0.0
    side: str = "LONG"                     # "LONG" | "SHORT"
    margin: float = 0.0                    # vadeli paper modda kilitli marjin (USDT)
    funding_acc: float = 0.0               # tahakkuk eden funding maliyeti (USDT)
    # --- kâr bildirimi (R = giriş anındaki stop mesafesi) ---
    # Eski kayıtlarda bu alanlar yoktur; dataclass varsayılanı sayesinde
    # Position(**eski_json) sorunsuz yüklenir.
    risk_unit: float = 0.0                 # giriş anındaki ATR×çarpan mesafesi = 1R
    r_notified: float = 0.0                # son bildirilen R seviyesi (tekrar bildirmemek için)


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
                CREATE TABLE IF NOT EXISTS equity_history (
                    ts TEXT PRIMARY KEY,
                    equity REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assessments (
                    symbol TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                -- Kâr kilometre taşları: pozisyon N×R kâra ulaştığında yazılır.
                -- Amaç: ileride "bildirim gelince kapattıklarım mı daha iyiydi,
                -- bıraktıklarım mı" sorusunu VERİYLE cevaplayabilmek.
                CREATE TABLE IF NOT EXISTS r_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    side TEXT,
                    r_level REAL,            -- bildirim eşiği (4, 8, ...)
                    r_actual REAL,           -- o andaki gerçek R çokluğu
                    price REAL,
                    upnl_usdt REAL,          -- o anki açık kâr
                    locked_usdt REAL         -- stop'un garantilediği kâr
                );
                """
            )
        # göç: eski kurulumlardaki trades tablosuna side sütunu ekle
        try:
            with self._conn:
                self._conn.execute("ALTER TABLE trades ADD COLUMN side TEXT DEFAULT 'LONG'")
        except sqlite3.OperationalError:
            pass  # sütun zaten var

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
        side: str = "LONG", pnl_override: Optional[float] = None,
    ) -> tuple[float, float]:
        if side == "SHORT":
            pnl_usdt = (entry_price - exit_price) * qty
            pnl_pct = (entry_price / exit_price - 1.0) * 100.0 if exit_price else 0.0
        else:
            pnl_usdt = (exit_price - entry_price) * qty
            pnl_pct = (exit_price / entry_price - 1.0) * 100.0 if entry_price else 0.0
        if pnl_override is not None:
            pnl_usdt = pnl_override  # komisyon+funding dahil net değer (vadeli paper mod)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trades(symbol, entry_time, exit_time, entry_price, exit_price,"
                " qty, pnl_usdt, pnl_pct, exit_reason, side) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (symbol, entry_time, exit_time, entry_price, exit_price, qty, pnl_usdt, pnl_pct,
                 exit_reason, side),
            )
        return pnl_usdt, pnl_pct

    def recent_trades(self, limit: int = 30) -> list[dict]:
        cur = self._conn.execute(
            "SELECT symbol, side, entry_time, exit_time, entry_price, exit_price,"
            " qty, pnl_usdt, pnl_pct, exit_reason FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------ varlık geçmişi
    def record_equity(self, ts_iso: str, equity: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO equity_history(ts, equity) VALUES(?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity",
                (ts_iso, equity),
            )

    def equity_history(self, limit: int = 600) -> list[tuple[str, float]]:
        cur = self._conn.execute(
            "SELECT ts, equity FROM equity_history ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return list(reversed(cur.fetchall()))

    def all_positions(self) -> list[Position]:
        cur = self._conn.execute("SELECT data FROM positions")
        return [Position(**json.loads(r[0])) for r in cur.fetchall()]

    # ------------------------------------------------------- kâr kilometre taşları
    def record_r_event(self, symbol: str, ts: str, side: str, r_level: float,
                       r_actual: float, price: float, upnl: float, locked: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO r_events(symbol, ts, side, r_level, r_actual, price,"
                " upnl_usdt, locked_usdt) VALUES(?,?,?,?,?,?,?,?)",
                (symbol, ts, side, r_level, r_actual, price, upnl, locked),
            )

    def recent_r_events(self, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            "SELECT symbol, ts, side, r_level, r_actual, price, upnl_usdt, locked_usdt"
            " FROM r_events ORDER BY id DESC LIMIT ?", (limit,))
        cols = ("symbol", "ts", "side", "r_level", "r_actual", "price",
                "upnl_usdt", "locked_usdt")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # -------------------------------------------------------- ön değerlendirmeler
    def save_assessment(self, symbol: str, ts_iso: str, data_json: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO assessments(symbol, ts, data) VALUES(?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts, data=excluded.data",
                (symbol, ts_iso, data_json),
            )

    def latest_assessments(self) -> list[dict]:
        cur = self._conn.execute("SELECT symbol, ts, data FROM assessments ORDER BY symbol")
        return [{"symbol": s, "ts": t, **json.loads(d)} for s, t, d in cur.fetchall()]

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
