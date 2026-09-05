"""Risk yönetimi: pozisyon boyutlandırma + günlük devre kesici.

Pozisyon boyutu üç ayrı sınırın EN KÜÇÜĞÜ ile belirlenir:
  1. Risk sınırı  : (bakiye × risk_pct) / stop mesafesi
  2. Bakiye sınırı: kullanılabilir USDT'nin %95'i / fiyat  (spotta kaldıraç yok!)
  3. Borsa filtreleri: stepSize'a AŞAĞI yuvarlama, minQty ve minNotional kontrolü
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .exchange import SymbolFilters, floor_to_step

log = logging.getLogger("risk")


@dataclass(frozen=True)
class SizingResult:
    qty: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.qty > 0


def position_size(
    usdt_free: float,
    price: float,
    stop_distance: float,
    risk_pct: float,
    max_balance_usage: float,
    filters: SymbolFilters,
) -> SizingResult:
    if stop_distance <= 0 or price <= 0:
        return SizingResult(0, "geçersiz stop mesafesi/fiyat")

    risk_amount = usdt_free * risk_pct
    qty_by_risk = risk_amount / stop_distance
    qty_by_balance = (usdt_free * max_balance_usage) / price

    raw_qty = min(qty_by_risk, qty_by_balance)
    qty = floor_to_step(raw_qty, filters.step_size)

    if qty < filters.min_qty:
        return SizingResult(0, f"miktar {qty} < minQty {filters.min_qty}")
    notional = qty * price
    if notional < filters.min_notional:
        return SizingResult(0, f"tutar {notional:.2f} USDT < minNotional {filters.min_notional}")
    return SizingResult(qty)


class CircuitBreaker:
    """Günlük gerçekleşen zarar limiti aşılırsa o gün yeni pozisyon açılmaz.

    Açık pozisyonların stopları borsada durmaya devam eder — devre kesici
    yalnızca YENİ girişleri durdurur, mevcut korumayı kaldırmaz.
    """

    def __init__(self, state, max_daily_loss_pct: float):
        self.state = state
        self.max_daily_loss_pct = max_daily_loss_pct

    def day_start_equity(self, today_iso: str, current_equity: float) -> float:
        stored_day = self.state.get_kv("cb_day")
        if stored_day != today_iso:
            self.state.set_kv("cb_day", today_iso)
            self.state.set_kv("cb_day_start_equity", f"{current_equity:.2f}")
            return current_equity
        return float(self.state.get_kv("cb_day_start_equity", f"{current_equity:.2f}"))

    def entries_allowed(self, today_iso: str, current_equity: float) -> tuple[bool, float]:
        """(giriş serbest mi, günlük PnL yüzdesi) döndürür."""
        start = self.day_start_equity(today_iso, current_equity)
        if start <= 0:
            return True, 0.0
        day_pnl_pct = (current_equity - start) / start
        if day_pnl_pct <= -self.max_daily_loss_pct:
            return False, day_pnl_pct
        return True, day_pnl_pct
