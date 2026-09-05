"""Strateji: EMA200 trend filtresi + Donchian kırılımı + ATR izleyen stop.

Bu modül HEM canlı bot HEM backtest tarafından kullanılır. Sinyal mantığı
tek yerde durur; böylece "backtest başka şeyi, canlı bot başka şeyi test
ediyor" sorunu ortadan kalkar.

Kurallar (yalnızca KAPANMIŞ mum verisiyle):
  GİRİŞ:  kapanış > EMA200                      (trend filtresi)
       VE kapanış > son N barın en yükseği       (Donchian kırılımı — momentum tetikleyici)
       VE RSI < rsi_max_entry                    (aşırı alım zirvesinde girme)
  ÇIKIŞ:  yalnızca ATR izleyen stop (borsa tarafında gerçek emir olarak durur)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import StrategyParams


# ---------------------------------------------------------------- indikatörler
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(100.0).where(avg_loss.notna(), np.nan)


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def compute_indicators(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """df sütunları: open, high, low, close, volume (float). Kopya döndürür."""
    out = df.copy()
    out["ema_trend"] = ema(out["close"], p.ema_period)
    out["rsi"] = rsi_wilder(out["close"], p.rsi_period)
    out["atr"] = atr_wilder(out["high"], out["low"], out["close"], p.atr_period)
    # shift(1): kırılım kanalı MEVCUT barı içermez (look-ahead önlenir)
    out["donchian_high"] = out["high"].rolling(p.donchian_period).max().shift(1)
    return out


# ------------------------------------------------------------------- sinyaller
@dataclass(frozen=True)
class EntrySignal:
    should_enter: bool
    close: float
    atr: float
    reason: str = ""


def check_entry(row: pd.Series, p: StrategyParams) -> EntrySignal:
    """Tek bir KAPANMIŞ mum satırı üzerinde giriş kararı verir."""
    close = float(row["close"])
    needed = (row["ema_trend"], row["rsi"], row["atr"], row["donchian_high"])
    if any(pd.isna(v) for v in needed):
        return EntrySignal(False, close, float("nan"), "indikatör ısınması tamamlanmadı")

    in_uptrend = close > float(row["ema_trend"])
    breakout = close > float(row["donchian_high"])
    rsi_ok = float(row["rsi"]) < p.rsi_max_entry

    if in_uptrend and breakout and rsi_ok:
        return EntrySignal(True, close, float(row["atr"]), "EMA200 üzeri + Donchian kırılımı")
    return EntrySignal(False, close, float(row["atr"]))


def initial_stop(entry_price: float, atr: float, p: StrategyParams) -> float:
    return entry_price - atr * p.atr_multiplier


def updated_trailing_stop(
    current_stop: float, highest_price: float, atr: float, p: StrategyParams
) -> float:
    """İzleyen stop yalnızca YÜKSELİR, asla düşmez."""
    candidate = highest_price - atr * p.atr_multiplier
    return max(current_stop, candidate)
