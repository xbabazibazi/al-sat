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
    out["donchian_low"] = out["low"].rolling(p.donchian_period).min().shift(1)
    # Ön değerlendirme araçları için: trend eğimi (ATR birimiyle) ve volatilite yüzdesi
    out["ema_slope"] = (out["ema_trend"] - out["ema_trend"].shift(10)) / out["atr"]
    out["atr_pct"] = out["atr"] / out["close"] * 100.0
    return out


def add_daily_trend_filter(ind: pd.DataFrame, daily_df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """Bar-içi (örn. 4h) DataFrame'e `daily_uptrend` sütunu ekler.

    Look-ahead önleme: her bar-içi mum, yalnızca KENDİ AÇILIŞINDAN ÖNCE
    KAPANMIŞ günlük mumun (close_time <= open_time) EMA durumunu görür.
    daily_df sütunları: open_time, close_time, close.
    """
    d = daily_df.copy()
    d["daily_ema"] = ema(d["close"].astype(float), p.daily_ema_period)
    d["daily_uptrend"] = d["close"].astype(float) > d["daily_ema"]
    d = d[["close_time", "daily_uptrend"]].rename(columns={"close_time": "d_close"})
    d = d.sort_values("d_close")

    out = ind.copy().sort_values("open_time")
    merged = pd.merge_asof(
        out, d, left_on="open_time", right_on="d_close", direction="backward"
    )
    merged["daily_uptrend"] = merged["daily_uptrend"].fillna(False).astype(bool)
    return merged.drop(columns=["d_close"])


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

    # Günlük trend teyidi (sütun yoksa filtre uygulanmaz — geriye uyumlu)
    daily_ok = True
    if p.use_daily_filter and "daily_uptrend" in row.index:
        daily_ok = bool(row["daily_uptrend"])

    if in_uptrend and breakout and rsi_ok and daily_ok:
        return EntrySignal(True, close, float(row["atr"]), "EMA200 üzeri + Donchian kırılımı + günlük trend")
    return EntrySignal(False, close, float(row["atr"]))


def check_entry_short(row: pd.Series, p: StrategyParams) -> EntrySignal:
    """Short girişi — long kurallarının aynadaki yansıması:
    EMA200 ALTINDA trend + Donchian ALT bandı kırılımı + aşırı satımda değil."""
    close = float(row["close"])
    needed = (row["ema_trend"], row["rsi"], row["atr"], row["donchian_low"])
    if any(pd.isna(v) for v in needed):
        return EntrySignal(False, close, float("nan"), "indikatör ısınması tamamlanmadı")

    in_downtrend = close < float(row["ema_trend"])
    breakdown = close < float(row["donchian_low"])
    rsi_ok = float(row["rsi"]) > (100.0 - p.rsi_max_entry)  # dip kapitülasyonunda girme

    daily_ok = True
    if p.use_daily_filter and "daily_uptrend" in row.index:
        daily_ok = not bool(row["daily_uptrend"])  # short için günlük trend de aşağı olmalı

    if in_downtrend and breakdown and rsi_ok and daily_ok:
        return EntrySignal(True, close, float(row["atr"]), "EMA200 altı + Donchian aşağı kırılımı")
    return EntrySignal(False, close, float(row["atr"]))


def initial_stop(entry_price: float, atr: float, p: StrategyParams) -> float:
    return entry_price - atr * p.atr_multiplier


def updated_trailing_stop(
    current_stop: float, highest_price: float, atr: float, p: StrategyParams
) -> float:
    """İzleyen stop yalnızca YÜKSELİR, asla düşmez."""
    candidate = highest_price - atr * p.atr_multiplier
    return max(current_stop, candidate)
