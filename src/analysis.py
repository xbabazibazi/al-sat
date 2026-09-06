"""Ön değerlendirme (pre-trade analysis) katmanı.

Kural: HİÇBİR işleme incelemesiz girilmez. Her sinyal anında 5 araç çalışır,
her araç yöne oy verir (+long / −short), bileşik skor eşiği geçilmezse işlem
REDDEDİLİR. Her değerlendirme — girilsin girilmesin — veritabanına yazılır ve
panelde görünür; botun neden girdiği/girmediği her zaman izlenebilir.

Araçlar ve ağırlıkları:
  1. 4s Trend (fiyat vs EMA200)............ ±30   ana rejim
  2. Günlük Trend (günlük EMA200).......... ±20   üst zaman dilimi teyidi
  3. Momentum (RSI bölgesi)................ ±15   45-55 arası nötr
  4. Trend Gücü (EMA200 eğimi / ATR)....... ±20   yatay piyasayı eler
  5. Volatilite Sağlığı (ATR%)............. VETO  ölü (<%0.3) veya kaotik (>%6)
                                                  piyasada işlem tümden engellenir

Karar: skor ≥ +50 → LONG uygun · skor ≤ −50 → SHORT uygun · arası → BEKLE.
Kırılım (Donchian) tetikleyicisi ayrıca şarttır — analiz yönü onaylar,
kırılım zamanlamayı belirler. Stop-loss her pozisyonda MUTLAKA vardır
(girişte ATR×çarpan mesafesine kurulur; stop'suz pozisyon açılamaz).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import pandas as pd

from .config import StrategyParams

# Eşik neden 20? (2026-09-06 ölçümü — bkz. docs/03-Karar-Gunlugu.md)
#
# Giriş sinyali ZATEN fiyatın EMA200'ün doğru tarafında olmasını şart koşar,
# dolayısıyla "4s Trend" oyu bir LONG sinyalinde her zaman +30, SHORT'ta −30
# gelir. Eşik 50 iken diğer araçlardan ±20 daha gerekir; günlük trend ters
# yöndeyse (∓20) kalan iki aracın toplamı (max ±35) buna asla yetişemez.
# Sonuç: eşik 50, günlük trend ters yönde olduğunda girişi TAMAMEN yasaklar —
# yani A/B testinde tutarsız bulunup USE_DAILY_FILTER=false ile kapatılan
# günlük EMA filtresini arka kapıdan geri sokar.
#
# Çürütülebilir kanıt (backtest/esik_saglamlik.py, 10 parite, 2022-2026):
#   eşik 50 -> günlük filtreyi AÇMAK sonucu 0.00pp değiştirir (10/10 parite aynı)
#   eşik 20 -> günlük filtreyi AÇMAK −8.03pp götürür (0/10 parite aynı)
# Yani 50'de filtre zaten uygulanıyordu.
#
# Üç kapılı sağlamlık: plato 0-25 arası düz (uçurum 30'da) · 9/10 paritede
# üstün · her iki dönemde de üstün. Getiri %11.8->%19.8, Sharpe 0.45->0.58,
# bedeli MaxDD %10.6->%12.0. Plato ORTASI seçildi (kenarı değil).
#
# Katman KALDIRILMADI: volatilite vetosu ve karşı-yön reddi aynen çalışır.
SCORE_THRESHOLD = int(os.getenv("ANALYSIS_THRESHOLD", "20"))
ATR_PCT_MIN = 0.3   # bunun altı: ölü piyasa, kırılımlar sahte olur
ATR_PCT_MAX = 6.0   # bunun üstü: kaos/haber şoku, stoplar anlamsızlaşır


@dataclass
class ToolVote:
    tool: str
    score: int          # pozitif = long yönü, negatif = short yönü
    detail: str


@dataclass
class Assessment:
    symbol: str
    score: int = 0
    decision: str = "BEKLE"       # "LONG-UYGUN" | "SHORT-UYGUN" | "BEKLE" | "VETO"
    veto_reason: str = ""
    votes: list[ToolVote] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "score": self.score, "decision": self.decision, "veto": self.veto_reason,
            "votes": [{"tool": v.tool, "score": v.score, "detail": v.detail} for v in self.votes],
        }, ensure_ascii=False)


def assess(symbol: str, row: pd.Series, p: StrategyParams,
           daily_uptrend: bool | None = None) -> Assessment:
    """Kapanmış mum verisi üzerinde 5 araçlı yön analizi."""
    a = Assessment(symbol=symbol)
    close = float(row["close"])

    needed = ("ema_trend", "rsi", "atr", "ema_slope", "atr_pct")
    if any(pd.isna(row.get(k)) for k in needed):
        a.decision = "VETO"
        a.veto_reason = "indikatör ısınması tamamlanmadı"
        return a

    # 5) Volatilite sağlığı — VETO aracı, yön oylamasından önce
    atr_pct = float(row["atr_pct"])
    if atr_pct < ATR_PCT_MIN:
        a.decision, a.veto_reason = "VETO", f"ölü piyasa (ATR %{atr_pct:.2f} < %{ATR_PCT_MIN})"
        return a
    if atr_pct > ATR_PCT_MAX:
        a.decision, a.veto_reason = "VETO", f"aşırı volatilite (ATR %{atr_pct:.2f} > %{ATR_PCT_MAX})"
        return a
    a.votes.append(ToolVote("Volatilite", 0, f"sağlıklı bant: ATR %{atr_pct:.2f}"))

    # 1) 4s trend
    ema = float(row["ema_trend"])
    if close > ema:
        a.votes.append(ToolVote("4s Trend", +30, f"fiyat EMA200'ün %{(close/ema-1)*100:.1f} üzerinde"))
    else:
        a.votes.append(ToolVote("4s Trend", -30, f"fiyat EMA200'ün %{(1-close/ema)*100:.1f} altında"))

    # 2) Günlük trend (veri yoksa oy kullanmaz)
    if daily_uptrend is True:
        a.votes.append(ToolVote("Günlük Trend", +20, "günlük kapanış > günlük EMA200"))
    elif daily_uptrend is False:
        a.votes.append(ToolVote("Günlük Trend", -20, "günlük kapanış < günlük EMA200"))
    else:
        a.votes.append(ToolVote("Günlük Trend", 0, "günlük veri yok — oy kullanmadı"))

    # 3) Momentum (RSI)
    rsi = float(row["rsi"])
    if rsi > 55:
        a.votes.append(ToolVote("Momentum", +15, f"RSI {rsi:.0f} — alıcılar baskın"))
    elif rsi < 45:
        a.votes.append(ToolVote("Momentum", -15, f"RSI {rsi:.0f} — satıcılar baskın"))
    else:
        a.votes.append(ToolVote("Momentum", 0, f"RSI {rsi:.0f} — nötr bölge"))

    # 4) Trend gücü (EMA eğimi, ATR birimiyle)
    slope = float(row["ema_slope"])
    if slope > 0.5:
        a.votes.append(ToolVote("Trend Gücü", +20, f"EMA200 belirgin yükseliyor (eğim {slope:+.2f} ATR)"))
    elif slope < -0.5:
        a.votes.append(ToolVote("Trend Gücü", -20, f"EMA200 belirgin düşüyor (eğim {slope:+.2f} ATR)"))
    else:
        a.votes.append(ToolVote("Trend Gücü", 0, f"EMA200 yatay (eğim {slope:+.2f} ATR)"))

    a.score = sum(v.score for v in a.votes)
    if a.score >= SCORE_THRESHOLD:
        a.decision = "LONG-UYGUN"
    elif a.score <= -SCORE_THRESHOLD:
        a.decision = "SHORT-UYGUN"
    else:
        a.decision = "BEKLE"
    return a


def analyzer_allows(a: Assessment, side: str) -> bool:
    """Giriş kapısı: analiz kararı yönle örtüşmüyorsa işlem REDDEDİLİR."""
    if side == "LONG":
        return a.decision == "LONG-UYGUN"
    if side == "SHORT":
        return a.decision == "SHORT-UYGUN"
    return False
