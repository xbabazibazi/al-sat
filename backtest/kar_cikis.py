"""Kâr realizasyon şemalarını karşılaştırır.

Soru: kârı nasıl realize etmeliyiz? Saf iz süren stop mu, kısmi çıkış mı,
başabaş stop mu, sert kâr hedefi mi?

Bu dosya futures_engine'in bir KOPYASI DEĞİL, genişletilmiş sürümüdür: aynı
komisyon/funding/likidasyon/slipaj varsayımları, aynı giriş mantığı; tek fark
pozisyondan çıkış şeması. Böylece karşılaştırma adil olur — değişen tek şey
test etmek istediğimiz değişken.

R birimi = giriş anındaki stop mesafesi (ATR × çarpan). "2R'de yarısını kapat"
demek, fiyat lehimize 2 stop mesafesi gittiğinde pozisyonun yarısını kapat
demektir. Risk %1 sabit olduğu için 1R ≈ sermayenin %1'i.

BAR İÇİ SIRALAMA KARAMSARDIR: aynı barda hem stop hem kâr hedefi mümkünse
STOP önce çalışmış sayılır. Bu, kısmi çıkış şemalarını kayırmamak içindir.

    python -m backtest.kar_cikis              # 10 canlı parite
    python -m backtest.kar_cikis --symbol SOLUSDT
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import analyzer_allows, assess  # noqa: E402
from src.config import CONFIG, StrategyParams  # noqa: E402
from src.strategy import (add_daily_trend_filter, check_entry,  # noqa: E402
                          check_entry_short, compute_indicators)
from backtest.data import download_klines  # noqa: E402
from backtest.futures_engine import (FUNDING_DAILY_LONG,  # noqa: E402
                                     FUNDING_DAILY_SHORT, LIQ_BUFFER,
                                     SLIPPAGE, TAKER_FEE)

CANLI_PARITELER = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOTUSDT",
                   "FILUSDT", "DOGEUSDT", "INJUSDT", "ARBUSDT", "OPUSDT"]


@dataclass(frozen=True)
class CikisSemasi:
    """Bir kâr realizasyon şeması."""
    ad: str
    # [(R seviyesi, kapatılacak ORİJİNAL qty oranı)] — sırayla tetiklenir
    kismi: tuple[tuple[float, float], ...] = ()
    basabas_r: float | None = None      # bu R'ye ulaşınca stop girişe çekilir
    sikilastir_r: float | None = None   # bu R'den sonra ATR çarpanı düşürülür
    siki_carpan: float | None = None
    tam_cikis_r: float | None = None    # sert kâr hedefi: hepsini kapat


@dataclass
class Islem:
    side: str
    pnl: float
    reason: str
    r_coklugu: float   # pnl / risk birimi (R cinsinden sonuç)


@dataclass
class Sonuc:
    ad: str
    baslangic: float
    bitis: float
    islemler: list[Islem] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    bars_per_year: float = 2190.0

    @property
    def getiri_pct(self) -> float:
        return (self.bitis / self.baslangic - 1) * 100

    @property
    def maxdd_pct(self) -> float:
        if self.equity.empty:
            return 0.0
        peak = self.equity.cummax()
        return float(((self.equity - peak) / peak).min() * 100)

    @property
    def sharpe(self) -> float:
        if len(self.equity) < 2:
            return 0.0
        r = self.equity.pct_change().dropna()
        return float(r.mean() / r.std() * np.sqrt(self.bars_per_year)) if r.std() else 0.0

    def satir(self) -> dict:
        t = self.islemler
        kazanan = [x for x in t if x.pnl > 0]
        kayip = [x for x in t if x.pnl < 0]
        kaz = sum(x.pnl for x in kazanan)
        kyp = -sum(x.pnl for x in kayip)
        return {
            "Şema": self.ad,
            "Getiri %": round(self.getiri_pct, 1),
            "MaxDD %": round(self.maxdd_pct, 1),
            "Sharpe": round(self.sharpe, 2),
            "İşlem": len(t),
            "Kazanma %": round(len(kazanan) / len(t) * 100, 1) if t else 0,
            "PF": round(kaz / kyp, 2) if kyp > 0 else float("inf"),
            "Ort.Kaz R": round(np.mean([x.r_coklugu for x in kazanan]), 2) if kazanan else 0,
            "Ort.Kyp R": round(np.mean([x.r_coklugu for x in kayip]), 2) if kayip else 0,
            "En iyi R": round(max((x.r_coklugu for x in t), default=0), 1),
            "Likid.": sum(1 for x in t if x.reason == "liq"),
        }


def calistir(
    df: pd.DataFrame,
    sema: CikisSemasi,
    params: StrategyParams,
    leverage: float = 2.0,
    risk_pct: float = 0.01,
    start_cash: float = 10_000.0,
    max_balance_usage: float = 0.95,
    fee: float = TAKER_FEE,
    bar_hours: float = 4.0,
    daily_df: pd.DataFrame | None = None,
    use_analyzer: bool = True,
) -> Sonuc:
    ind = compute_indicators(df, params)
    if daily_df is not None and (params.use_daily_filter or use_analyzer):
        ind = add_daily_trend_filter(ind, daily_df, params)

    cash = start_cash
    side = ""
    qty = margin = entry_price = extreme = stop = funding_acc = 0.0
    orig_qty = risk_birimi = entry_fee_top = realize_kismi = 0.0
    kismi_yapildi: set[int] = set()
    basabas_ok = sikilastir_ok = False

    islemler: list[Islem] = []
    equity = np.full(len(ind), np.nan)

    opens = ind["open"].to_numpy(); highs = ind["high"].to_numpy()
    lows = ind["low"].to_numpy(); closes = ind["close"].to_numpy()
    atrs = ind["atr"].to_numpy()
    fund = {"LONG": FUNDING_DAILY_LONG, "SHORT": FUNDING_DAILY_SHORT}

    def lehte_r(bar_i: int) -> float:
        """Bu barda ulaşılan EN İYİ R çokluğu (uç fiyatla)."""
        if risk_birimi <= 0:
            return 0.0
        if side == "LONG":
            return (highs[bar_i] - entry_price) / risk_birimi
        return (entry_price - lows[bar_i]) / risk_birimi

    def kapat_kismi(oran: float, hedef: float) -> None:
        """Orijinal qty'nin `oran` kadarını `hedef` fiyattan kapatır."""
        nonlocal cash, qty, margin, funding_acc, realize_kismi
        q = min(orig_qty * oran, qty)
        if q <= 0:
            return
        fill = hedef * (1 - SLIPPAGE) if side == "LONG" else hedef * (1 + SLIPPAGE)
        raw = q * (fill - entry_price) if side == "LONG" else q * (entry_price - fill)
        pay = q / qty
        m_ser = margin * pay
        f_ser = funding_acc * pay
        cikis_fee = q * fill * fee
        cash += m_ser + raw - cikis_fee - f_ser
        realize_kismi += raw - cikis_fee - f_ser
        margin -= m_ser
        funding_acc -= f_ser
        qty -= q

    def kapat_tam(fill: float, reason: str) -> None:
        nonlocal cash, side, qty, margin, funding_acc, realize_kismi
        nonlocal orig_qty, risk_birimi, entry_fee_top, basabas_ok, sikilastir_ok
        if reason == "liq":
            pnl = realize_kismi - margin - entry_fee_top - funding_acc
        else:
            raw = qty * (fill - entry_price) if side == "LONG" else qty * (entry_price - fill)
            cikis_fee = qty * fill * fee
            cash += margin + raw - cikis_fee - funding_acc
            pnl = realize_kismi + raw - cikis_fee - funding_acc - entry_fee_top
        r_cok = pnl / (orig_qty * risk_birimi) if orig_qty * risk_birimi > 0 else 0.0
        islemler.append(Islem(side, pnl, reason, r_cok))
        side = ""
        qty = margin = funding_acc = realize_kismi = 0.0
        orig_qty = risk_birimi = entry_fee_top = 0.0
        kismi_yapildi.clear()
        basabas_ok = sikilastir_ok = False

    for i in range(params.warmup_bars, len(ind)):
        if side:
            funding_acc += qty * entry_price * fund[side] * (bar_hours / 24.0)
            liq_move = LIQ_BUFFER / leverage

            # --- 1) likidasyon > 2) stop  (KARAMSAR: stop kâr hedefinden önce) ---
            if side == "LONG":
                liq = entry_price * (1 - liq_move)
                if leverage > 1 and lows[i] <= liq:
                    kapat_tam(liq, "liq")
                elif opens[i] <= stop:
                    kapat_tam(opens[i] * (1 - SLIPPAGE), "stop")
                elif lows[i] <= stop:
                    kapat_tam(stop * (1 - SLIPPAGE), "stop")
            else:
                liq = entry_price * (1 + liq_move)
                if leverage > 1 and highs[i] >= liq:
                    kapat_tam(liq, "liq")
                elif opens[i] >= stop:
                    kapat_tam(opens[i] * (1 + SLIPPAGE), "stop")
                elif highs[i] >= stop:
                    kapat_tam(stop * (1 + SLIPPAGE), "stop")

        # --- 3) sert kâr hedefi (varsa) ---
        if side and sema.tam_cikis_r and lehte_r(i) >= sema.tam_cikis_r:
            hedef = (entry_price + sema.tam_cikis_r * risk_birimi if side == "LONG"
                     else entry_price - sema.tam_cikis_r * risk_birimi)
            kapat_tam(hedef * (1 - SLIPPAGE) if side == "LONG" else hedef * (1 + SLIPPAGE),
                      "kâr hedefi")

        # --- 4) kısmi çıkışlar ---
        if side and sema.kismi:
            ulasilan = lehte_r(i)
            for idx, (r_sev, oran) in enumerate(sema.kismi):
                if idx in kismi_yapildi or ulasilan < r_sev:
                    continue
                hedef = (entry_price + r_sev * risk_birimi if side == "LONG"
                         else entry_price - r_sev * risk_birimi)
                kapat_kismi(oran, hedef)
                kismi_yapildi.add(idx)
            if qty <= 1e-12 and side:      # kısmiler pozisyonu bitirdiyse
                kapat_tam(closes[i], "kısmi tamam")

        # --- 5) başabaş stop / sıkılaştırma ---
        if side:
            ulasilan = lehte_r(i)
            if sema.basabas_r and not basabas_ok and ulasilan >= sema.basabas_r:
                stop = max(stop, entry_price) if side == "LONG" else min(stop, entry_price)
                basabas_ok = True
            if sema.sikilastir_r and not sikilastir_ok and ulasilan >= sema.sikilastir_r:
                sikilastir_ok = True

        # --- 6) iz süren stop güncellemesi ---
        if side and not np.isnan(atrs[i]):
            carpan = (sema.siki_carpan if (sikilastir_ok and sema.siki_carpan)
                      else params.atr_multiplier)
            if side == "LONG":
                extreme = max(extreme, highs[i])
                stop = max(stop, extreme - atrs[i] * carpan)
            else:
                extreme = min(extreme, lows[i])
                stop = min(stop, extreme + atrs[i] * carpan)

        # --- 7) giriş ---
        if not side and i > 0:
            prev = ind.iloc[i - 1]
            sig_l = check_entry(prev, params)
            sig_s = check_entry_short(prev, params)
            yon = "LONG" if (sig_l and sig_l.should_enter) else (
                  "SHORT" if (sig_s and sig_s.should_enter) else "")
            if yon and use_analyzer:
                daily_up = bool(prev["daily_uptrend"]) if "daily_uptrend" in prev.index else None
                if not analyzer_allows(assess("BT", prev, params, daily_up), yon):
                    yon = ""
            if yon:
                sig = sig_l if yon == "LONG" else sig_s
                fill = opens[i] * (1 + SLIPPAGE) if yon == "LONG" else opens[i] * (1 - SLIPPAGE)
                stop_mesafe = sig.atr * params.atr_multiplier
                if stop_mesafe > 0:
                    q = min((cash * risk_pct) / stop_mesafe,
                            (cash * max_balance_usage * leverage) / fill)
                    notional = q * fill
                    m = notional / leverage
                    if q > 0 and notional >= 10 and m <= cash * max_balance_usage:
                        entry_fee_top = notional * fee
                        cash -= m + entry_fee_top
                        side = yon; qty = orig_qty = q; margin = m
                        entry_price = extreme = fill
                        risk_birimi = stop_mesafe
                        funding_acc = realize_kismi = 0.0
                        kismi_yapildi.clear()
                        basabas_ok = sikilastir_ok = False
                        stop = fill - stop_mesafe if yon == "LONG" else fill + stop_mesafe

        # --- 8) bar sonu değerleme ---
        upnl = 0.0
        if side == "LONG":
            upnl = qty * (closes[i] - entry_price)
        elif side == "SHORT":
            upnl = qty * (entry_price - closes[i])
        equity[i] = cash + margin + upnl - funding_acc

    if side:
        kapat_tam(closes[-1] * (1 - SLIPPAGE) if side == "LONG" else closes[-1] * (1 + SLIPPAGE),
                  "eod")
        equity[-1] = cash

    return Sonuc(sema.ad, start_cash, cash, islemler, pd.Series(equity).dropna())


# --------------------------------------------------------------- şemalar
SEMALAR = [
    CikisSemasi("A) Mevcut: saf iz süren stop"),
    CikisSemasi("B) %50 @1R + iz süren", kismi=((1.0, 0.50),)),
    CikisSemasi("C) %50 @2R + iz süren", kismi=((2.0, 0.50),)),
    CikisSemasi("D) %50 @3R + iz süren", kismi=((3.0, 0.50),)),
    CikisSemasi("E) %33 @2R, %33 @4R + iz", kismi=((2.0, 1/3), (4.0, 1/3))),
    CikisSemasi("F) %50 @2R + başabaş stop", kismi=((2.0, 0.50),), basabas_r=2.0),
    CikisSemasi("G) Başabaş stop @1R", basabas_r=1.0),
    CikisSemasi("H) 2R sonrası stop sıkılaşır", sikilastir_r=2.0, siki_carpan=1.5),
    CikisSemasi("I) Sert kâr hedefi @3R", tam_cikis_r=3.0),
    CikisSemasi("J) Sert kâr hedefi @5R", tam_cikis_r=5.0),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="tek parite (varsayılan: 10 canlı parite)")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-01-01")
    args = ap.parse_args()

    semboller = [args.symbol] if args.symbol else CANLI_PARITELER
    p = CONFIG.strategy
    print(f"Canlı ayarlar: Donchian={p.donchian_period} ATR×{p.atr_multiplier} "
          f"kaldıraç={CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f} analiz=AÇIK")
    print(f"Dönem: {args.start} → {args.end} (4h)\n")

    toplam: dict[str, list[dict]] = {s.ad: [] for s in SEMALAR}

    for sym in semboller:
        df = download_klines(sym, "4h", args.start, args.end)
        daily = download_klines(sym, "1d", "2021-02-15", "2026-01-01")
        satirlar = []
        for sema in SEMALAR:
            r = calistir(df, sema, p, leverage=CONFIG.leverage,
                         risk_pct=CONFIG.risk_pct, daily_df=daily)
            s = r.satir()
            satirlar.append(s)
            toplam[sema.ad].append(s)
        print(f"\n=== {sym} ===")
        print(pd.DataFrame(satirlar).to_string(index=False))

    if len(semboller) > 1:
        print("\n\n" + "=" * 100)
        print(f"  {len(semboller)} PARİTE ORTALAMASI — kâr realizasyon şemaları")
        print("=" * 100)
        ozet = []
        for ad, rows in toplam.items():
            pf = [r["PF"] for r in rows if np.isfinite(r["PF"])]
            ozet.append({
                "Şema": ad,
                "Ort.Getiri %": round(np.mean([r["Getiri %"] for r in rows]), 1),
                "Medyan Getiri %": round(np.median([r["Getiri %"] for r in rows]), 1),
                "Pozitif": f"{sum(1 for r in rows if r['Getiri %'] > 0)}/{len(rows)}",
                "Ort.MaxDD %": round(np.mean([r["MaxDD %"] for r in rows]), 1),
                "Ort.Sharpe": round(np.mean([r["Sharpe"] for r in rows]), 2),
                "Ort.Kazanma %": round(np.mean([r["Kazanma %"] for r in rows]), 1),
                "Ort.PF": round(np.mean(pf), 2) if pf else 0,
                "Ort.Kaz R": round(np.mean([r["Ort.Kaz R"] for r in rows]), 2),
                "Toplam Likid.": sum(r["Likid."] for r in rows),
            })
        print(pd.DataFrame(ozet).to_string(index=False))


if __name__ == "__main__":
    main()
