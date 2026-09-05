"""Backtest motoru — canlı botun davranışını birebir taklit eder.

Canlı bot ile eşleşen kurallar:
- Sinyal, KAPANMIŞ mumun indikatörleriyle üretilir; alım bir SONRAKİ mumun
  açılışında gerçekleşir (canlıda mum kapanışından saniyeler sonra market emri).
- Stop borsa tarafında durduğu için BAR İÇİ (low) tetiklenir — sadece kapanışa
  bakan naif backtestlerin aksine.
- İzleyen stop yalnızca mum KAPANIŞINDA yükseltilir (canlıdaki akışla aynı).
- Komisyon (%0.1) ve kayma her iki yönde uygulanır.
- Pozisyon boyutu canlıdaki formülle aynı: min(risk/stop_mesafesi, bakiye*0.95/fiyat).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.config import StrategyParams
from src.strategy import check_entry, compute_indicators, initial_stop, updated_trailing_stop

FEE = 0.001         # %0.1 Binance spot komisyonu (taraf başına)
SLIPPAGE = 0.0005   # %0.05 kayma (taraf başına)


@dataclass
class Trade:
    entry_i: int
    exit_i: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float


@dataclass
class BacktestResult:
    params: dict
    start_cash: float
    end_equity: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    bars_per_year: float = 8760.0  # 1h varsayılanı; 4h→2190, 1d→365

    # ---- metrikler ----
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
    def win_rate_pct(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades) * 100

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return gains / losses if losses > 0 else float("inf")

    @property
    def sharpe(self) -> float:
        eq = self.equity_curve
        if len(eq) < 2:
            return 0.0
        rets = eq.pct_change().dropna()
        if rets.std() == 0:
            return 0.0
        return float(rets.mean() / rets.std() * np.sqrt(self.bars_per_year))

    def summary_row(self) -> dict:
        return {
            **self.params,
            "Getiri %": round(self.total_return_pct, 1),
            "MaxDD %": round(self.max_drawdown_pct, 1),
            "Sharpe": round(self.sharpe, 2),
            "İşlem": len(self.trades),
            "Kazanma %": round(self.win_rate_pct, 1),
            "PF": round(self.profit_factor, 2) if self.profit_factor != float("inf") else "∞",
        }


def run_backtest(
    df: pd.DataFrame,
    params: StrategyParams,
    start_cash: float = 10_000.0,
    risk_pct: float = 0.02,
    max_balance_usage: float = 0.95,
    label: dict | None = None,
    bars_per_year: float = 8760.0,
) -> BacktestResult:
    ind = compute_indicators(df, params)

    cash = start_cash
    qty = 0.0
    entry_price = highest = stop = 0.0
    entry_i = -1
    trades: list[Trade] = []
    equity = np.full(len(ind), np.nan)

    opens = ind["open"].to_numpy()
    highs = ind["high"].to_numpy()
    lows = ind["low"].to_numpy()
    closes = ind["close"].to_numpy()
    atrs = ind["atr"].to_numpy()

    for i in range(params.warmup_bars, len(ind)):
        # ---------- pozisyondayken: bar içi stop kontrolü (borsa tarafı stop) ----------
        if qty > 0:
            exit_price = None
            if opens[i] <= stop:            # bar stopun altında açıldı (gap) → açılışta çık
                exit_price = opens[i]
            elif lows[i] <= stop:           # bar içinde stopa değdi → stop fiyatından çık
                exit_price = stop
            if exit_price is not None:
                exit_price *= (1 - SLIPPAGE)
                cash += qty * exit_price * (1 - FEE)
                # PnL komisyon DAHİL (net) — metrikler gerçeği yansıtsın
                net_pnl = qty * (exit_price * (1 - FEE) - entry_price * (1 + FEE))
                trades.append(Trade(entry_i, i, entry_price, exit_price, qty, net_pnl))
                qty = 0.0

        # ---------- bar kapanışı: izleyen stopu güncelle ----------
        if qty > 0:
            highest = max(highest, highs[i])
            if not np.isnan(atrs[i]):
                stop = updated_trailing_stop(stop, highest, atrs[i], params)

        # ---------- düz durumda: önceki KAPANMIŞ barın sinyaliyle bu barın açılışında gir ----------
        if qty == 0 and i > 0:
            sig = check_entry(ind.iloc[i - 1], params)
            if sig.should_enter:
                fill = opens[i] * (1 + SLIPPAGE)
                stop_distance = sig.atr * params.atr_multiplier
                if stop_distance > 0:
                    q = min((cash * risk_pct) / stop_distance,
                            (cash * max_balance_usage) / fill)
                    cost = q * fill
                    if q > 0 and cost >= 10:  # minNotional benzeri alt sınır
                        cash -= cost * (1 + FEE)
                        qty = q
                        entry_price = fill
                        highest = fill
                        stop = initial_stop(fill, sig.atr, params)
                        entry_i = i

        equity[i] = cash + qty * closes[i]

    # açık pozisyonu son kapanışta kapat
    if qty > 0:
        exit_price = closes[-1] * (1 - SLIPPAGE)
        cash += qty * exit_price * (1 - FEE)
        net_pnl = qty * (exit_price * (1 - FEE) - entry_price * (1 + FEE))
        trades.append(Trade(entry_i, len(ind) - 1, entry_price, exit_price, qty, net_pnl))

    eq_series = pd.Series(equity).dropna()
    return BacktestResult(
        params=label or {"ATR×": params.atr_multiplier},
        start_cash=start_cash,
        end_equity=cash,
        trades=trades,
        equity_curve=eq_series,
        bars_per_year=bars_per_year,
    )
