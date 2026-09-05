"""Vadeli işlem (USDT-M futures) backtest motoru: long + short + kaldıraç.

Spot motorundan farkları:
- İki yönlü işlem: long VE short pozisyonlar (Donchian alt/üst kırılımı)
- Marjin muhasebesi: pozisyon için notional/L kadar marjin kilitlenir
- Funding maliyeti: pozisyon tutulan her gün notional üzerinden tahakkuk eder
  (long öder; short için de muhafazakâr küçük maliyet varsayılır — funding
  ayı piyasasında sık sık negatife döner ve short ödeyen taraf olur)
- Likidasyon: izole marjinde giriş fiyatından ~%90/L ters hareket marjini
  sıfırlar (bakım marjini payı). Stop emri likidasyondan önce kurtarır ama
  GAP durumunda likidasyon stop'tan önce gelebilir — bar bazında modellenir.
- Vadeli komisyonları: taker %0.05 varsayılan (spot %0.10'un yarısı)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.analysis import analyzer_allows, assess
from src.config import StrategyParams
from src.strategy import (add_daily_trend_filter, check_entry, check_entry_short,
                          compute_indicators, updated_trailing_stop)

TAKER_FEE = 0.0005          # %0.05 USDT-M taker (BNB indirimi hariç)
SLIPPAGE = 0.0005
FUNDING_DAILY_LONG = 0.0003   # %0.01 / 8s → günde %0.03 (long öder, tarihsel ortalama)
FUNDING_DAILY_SHORT = 0.00015 # muhafazakâr: short da yarısı kadar öder varsayımı
LIQ_BUFFER = 0.90             # likidasyon: %90/L ters hareket (bakım marjini payı)


@dataclass
class FTrade:
    side: str          # "LONG" | "SHORT"
    entry_i: int
    exit_i: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float         # komisyon + funding dahil net
    exit_reason: str   # "stop" | "liq" | "eod"


@dataclass
class FuturesResult:
    params: dict
    start_cash: float
    end_equity: float
    trades: list[FTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    bars_per_year: float = 2190.0

    @property
    def total_return_pct(self) -> float:
        return (self.end_equity / self.start_cash - 1) * 100

    @property
    def max_drawdown_pct(self) -> float:
        eq = self.equity_curve
        if eq.empty:
            return 0.0
        peak = eq.cummax()
        return float(((eq - peak) / peak).min() * 100)

    @property
    def sharpe(self) -> float:
        eq = self.equity_curve
        if len(eq) < 2:
            return 0.0
        rets = eq.pct_change().dropna()
        if rets.std() == 0:
            return 0.0
        return float(rets.mean() / rets.std() * np.sqrt(self.bars_per_year))

    @property
    def n_liquidations(self) -> int:
        return sum(1 for t in self.trades if t.exit_reason == "liq")

    def summary_row(self) -> dict:
        wins = sum(1 for t in self.trades if t.pnl > 0)
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        longs = [t for t in self.trades if t.side == "LONG"]
        shorts = [t for t in self.trades if t.side == "SHORT"]
        return {
            **self.params,
            "Getiri %": round(self.total_return_pct, 1),
            "MaxDD %": round(self.max_drawdown_pct, 1),
            "Sharpe": round(self.sharpe, 2),
            "İşlem": len(self.trades),
            "Long/Short": f"{len(longs)}/{len(shorts)}",
            "Long PnL": round(sum(t.pnl for t in longs), 0),
            "Short PnL": round(sum(t.pnl for t in shorts), 0),
            "Kazanma %": round(wins / len(self.trades) * 100, 1) if self.trades else 0,
            "PF": round(gains / losses, 2) if losses > 0 else "∞",
            "Likid.": self.n_liquidations,
        }


def run_futures_backtest(
    df: pd.DataFrame,
    params: StrategyParams,
    leverage: float = 1.0,
    allow_long: bool = True,
    allow_short: bool = True,
    start_cash: float = 10_000.0,
    risk_pct: float = 0.02,
    max_balance_usage: float = 0.95,
    fee: float = TAKER_FEE,
    bar_hours: float = 4.0,
    label: dict | None = None,
    bars_per_year: float = 2190.0,
    daily_df: pd.DataFrame | None = None,
    use_analyzer: bool = False,
) -> FuturesResult:
    ind = compute_indicators(df, params)
    # Analiz katmanının günlük-trend oyu için sütun eklenir; check_entry'nin
    # kendi günlük filtresi yalnızca params.use_daily_filter=True ise devreye girer.
    if daily_df is not None and (params.use_daily_filter or use_analyzer):
        ind = add_daily_trend_filter(ind, daily_df, params)

    cash = start_cash          # serbest bakiye (marjin dışı)
    side = ""                  # "" | "LONG" | "SHORT"
    qty = margin = entry_price = extreme = stop = funding_acc = 0.0
    entry_i = -1
    trades: list[FTrade] = []
    equity = np.full(len(ind), np.nan)

    opens = ind["open"].to_numpy(); highs = ind["high"].to_numpy()
    lows = ind["low"].to_numpy(); closes = ind["close"].to_numpy()
    atrs = ind["atr"].to_numpy()

    fund_daily = {"LONG": FUNDING_DAILY_LONG, "SHORT": FUNDING_DAILY_SHORT}

    def close_position(i: int, exit_price: float, reason: str) -> None:
        nonlocal cash, side, qty, margin, funding_acc
        raw = qty * (exit_price - entry_price) if side == "LONG" else qty * (entry_price - exit_price)
        fees = qty * (entry_price + exit_price) * fee
        pnl = raw - fees - funding_acc
        if reason == "liq":
            pnl = -margin - qty * entry_price * fee - funding_acc  # marjinin tamamı gider
            cash += 0.0
        else:
            cash += margin + raw - qty * exit_price * fee - funding_acc
        trades.append(FTrade(side, entry_i, i, entry_price, exit_price, qty, pnl, reason))
        side = ""; qty = margin = funding_acc = 0.0

    for i in range(params.warmup_bars, len(ind)):
        # ---------------- pozisyondayken: likidasyon > stop > iz süren güncelleme ----------------
        if side:
            funding_acc += qty * entry_price * fund_daily[side] * (bar_hours / 24.0)
            liq_move = LIQ_BUFFER / leverage

            if side == "LONG":
                liq_price = entry_price * (1 - liq_move)
                if leverage > 1 and lows[i] <= liq_price:
                    close_position(i, liq_price, "liq")
                elif opens[i] <= stop:
                    close_position(i, opens[i] * (1 - SLIPPAGE), "stop")
                elif lows[i] <= stop:
                    close_position(i, stop * (1 - SLIPPAGE), "stop")
            else:  # SHORT
                liq_price = entry_price * (1 + liq_move)
                if leverage > 1 and highs[i] >= liq_price:
                    close_position(i, liq_price, "liq")
                elif opens[i] >= stop:
                    close_position(i, opens[i] * (1 + SLIPPAGE), "stop")
                elif highs[i] >= stop:
                    close_position(i, stop * (1 + SLIPPAGE), "stop")

        if side and not np.isnan(atrs[i]):
            if side == "LONG":
                extreme = max(extreme, highs[i])
                stop = updated_trailing_stop(stop, extreme, atrs[i], params)
            else:
                extreme = min(extreme, lows[i])
                stop = min(stop, extreme + atrs[i] * params.atr_multiplier)

        # ---------------- düz: önceki kapanmış barın sinyaliyle bu barın açılışında gir ----------------
        if not side and i > 0:
            prev = ind.iloc[i - 1]
            sig_l = check_entry(prev, params) if allow_long else None
            sig_s = check_entry_short(prev, params) if allow_short else None
            direction = "LONG" if (sig_l and sig_l.should_enter) else (
                        "SHORT" if (sig_s and sig_s.should_enter) else "")
            if direction and use_analyzer:
                daily_up = bool(prev["daily_uptrend"]) if "daily_uptrend" in prev.index else None
                if not analyzer_allows(assess("BT", prev, params, daily_up), direction):
                    direction = ""  # inceleme onaylamadı — giriş reddedildi
            if direction:
                sig = sig_l if direction == "LONG" else sig_s
                fill = opens[i] * (1 + SLIPPAGE) if direction == "LONG" else opens[i] * (1 - SLIPPAGE)
                stop_distance = sig.atr * params.atr_multiplier
                if stop_distance > 0:
                    q = min((cash * risk_pct) / stop_distance,
                            (cash * max_balance_usage * leverage) / fill)
                    notional = q * fill
                    m = notional / leverage
                    if q > 0 and notional >= 10 and m <= cash * max_balance_usage:
                        cash -= m + notional * fee
                        side = direction; qty = q; margin = m
                        entry_price = fill; extreme = fill; entry_i = i; funding_acc = 0.0
                        stop = (fill - stop_distance) if direction == "LONG" else (fill + stop_distance)

        # ---------------- bar sonu değerleme ----------------
        upnl = 0.0
        if side == "LONG":
            upnl = qty * (closes[i] - entry_price)
        elif side == "SHORT":
            upnl = qty * (entry_price - closes[i])
        equity[i] = cash + margin + upnl - funding_acc

    if side:
        close_position(len(ind) - 1,
                       closes[-1] * (1 - SLIPPAGE) if side == "LONG" else closes[-1] * (1 + SLIPPAGE),
                       "eod")
        equity[-1] = cash

    return FuturesResult(
        params=label or {},
        start_cash=start_cash,
        end_equity=cash,
        trades=trades,
        equity_curve=pd.Series(equity).dropna(),
        bars_per_year=bars_per_year,
    )
