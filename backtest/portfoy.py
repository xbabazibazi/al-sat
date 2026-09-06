"""PORTFÖY backtest'i — 10 parite TEK bakiyeyi paylaşır (canlı botun gerçeği).

NEDEN YAZILDI: Şimdiye kadarki tüm ölçümler her pariteyi AYRI $10.000 ile
çalıştırdı. Canlı bot ise tek bakiyeyle 10 pariteyi yönetiyor. Aradaki fark
masum değil:

  1) KORELASYON — kripto varlıkları birlikte hareket eder. "İşlem başına %1
     risk" kulağa küçük gelir, ama 10 pozisyon aynı anda ve aynı yöne açıksa
     tek bir BTC şokunda %10'un tamamı birden gider. Ayrı-bakiye backtest'i
     bu riski YAPISAL OLARAK GÖREMEZ.
  2) SERMAYE REKABETİ — marjin paylaşıldığı için geç gelen sinyal parasız
     kalabilir; ayrı-bakiye testinde her sinyal daima finanse edilir.
  3) DEVRE KESİCİ — günlük sermaye stopu portföy geneline uygulanır, tek
     paritenin kendi hesabına değil.

Bu motor üçünü de modeller. Ayrıca iki koruma opsiyonel olarak test edilir:
  max_positions      — aynı anda en fazla kaç pozisyon
  max_net_exposure   — aynı yöndeki (net) pozisyon sayısı tavanı

    python -m backtest.portfoy
    python -m backtest.portfoy --limitler     # koruma senaryolarını karşılaştır
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
from backtest.kar_cikis import CANLI_PARITELER  # noqa: E402


@dataclass
class Poz:
    side: str
    qty: float
    entry: float
    margin: float
    stop: float
    extreme: float
    funding: float = 0.0
    risk_birimi: float = 0.0
    entry_fee: float = 0.0
    orig_qty: float = 0.0          # kısmi çıkış oranları buna göre hesaplanır
    realize_kismi: float = 0.0     # kısmi çıkışlardan bankaya yazılan net
    kismi_bitti: set = field(default_factory=set)


@dataclass
class PortfoySonuc:
    ad: str
    baslangic: float
    bitis: float
    islemler: list = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    max_es_zamanli: int = 0
    ort_es_zamanli: float = 0.0
    max_ayni_yon: int = 0
    devre_kesici_gun: int = 0

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
        return float(r.mean() / r.std() * np.sqrt(2190)) if r.std() else 0.0

    def satir(self) -> dict:
        t = self.islemler
        kaz = [x for x in t if x["pnl"] > 0]
        kyp = [x for x in t if x["pnl"] < 0]
        g = sum(x["pnl"] for x in kaz); k = -sum(x["pnl"] for x in kyp)
        return {
            "Senaryo": self.ad,
            "Getiri %": round(self.getiri_pct, 1),
            "MaxDD %": round(self.maxdd_pct, 1),
            "Sharpe": round(self.sharpe, 2),
            "İşlem": len(t),
            "Kazanma %": round(len(kaz) / len(t) * 100, 1) if t else 0,
            "PF": round(g / k, 2) if k > 0 else float("inf"),
            "Maks eşzamanlı": self.max_es_zamanli,
            "Ort eşzamanlı": round(self.ort_es_zamanli, 1),
            "Maks aynı yön": self.max_ayni_yon,
            "Devre kesici (gün)": self.devre_kesici_gun,
            "Likid.": sum(1 for x in t if x["reason"] == "liq"),
        }


def veri_hazirla(semboller, params, start, end):
    """Her sembol için indikatörlü DataFrame; open_time indeksli."""
    out = {}
    for s in semboller:
        df = download_klines(s, "4h", start, end)
        daily = download_klines(s, "1d", "2021-02-15", "2026-01-01")
        ind = compute_indicators(df, params)
        ind = add_daily_trend_filter(ind, daily, params)
        ind = ind.set_index("open_time", drop=False)
        out[s] = ind
    return out


def run_portfoy(
    veri: dict[str, pd.DataFrame],
    params: StrategyParams,
    ad: str = "portföy",
    leverage: float = 1.0,
    risk_pct: float = 0.01,
    start_cash: float = 10_000.0,
    max_balance_usage: float = 0.95,
    max_daily_loss_pct: float = 0.10,
    fee: float = TAKER_FEE,
    bar_hours: float = 4.0,
    use_analyzer: bool = True,
    max_positions: int | None = None,
    max_ayni_yon: int | None = None,
    tam_cikis_r: float | None = None,
    kismi_hedefler: tuple[tuple[float, float], ...] = (),
    kismi_sonrasi_basabas: bool = False,
) -> PortfoySonuc:
    semboller = list(veri.keys())
    zamanlar = sorted(set().union(*[set(d.index) for d in veri.values()]))

    cash = start_cash
    poz: dict[str, Poz] = {}
    islemler = []
    eq_kayit = []
    fund = {"LONG": FUNDING_DAILY_LONG, "SHORT": FUNDING_DAILY_SHORT}

    gun_bas_equity = start_cash
    gun_realize = 0.0
    su_anki_gun = None
    devre_kesici_gunler = set()
    es_zamanli_kayit = []
    max_es = 0
    max_yon = 0

    def equity_hesapla(t):
        e = cash
        for s, p in poz.items():
            d = veri[s]
            if t not in d.index:
                e += p.margin
                continue
            c = float(d.at[t, "close"])
            upnl = p.qty * (c - p.entry) if p.side == "LONG" else p.qty * (p.entry - c)
            e += p.margin + upnl - p.funding
        return e

    def kapat_kismi(s, oran, hedef):
        """Orijinal miktarın `oran` kadarını `hedef` fiyattan kapatır."""
        nonlocal cash, gun_realize
        p = poz[s]
        q = min(p.orig_qty * oran, p.qty)
        if q <= 0:
            return
        fill = hedef * (1 - SLIPPAGE) if p.side == "LONG" else hedef * (1 + SLIPPAGE)
        raw = q * (fill - p.entry) if p.side == "LONG" else q * (p.entry - fill)
        pay = q / p.qty
        m_ser = p.margin * pay
        f_ser = p.funding * pay
        cikis_fee = q * fill * fee
        cash += m_ser + raw - cikis_fee - f_ser
        net = raw - cikis_fee - f_ser
        p.realize_kismi += net
        gun_realize += net
        p.margin -= m_ser
        p.funding -= f_ser
        p.qty -= q

    def kapat(s, fiyat, reason, t):
        nonlocal cash, gun_realize
        p = poz[s]
        if reason == "liq":
            pnl = p.realize_kismi - p.margin - p.entry_fee - p.funding
        else:
            raw = p.qty * (fiyat - p.entry) if p.side == "LONG" else p.qty * (p.entry - fiyat)
            cikis_fee = p.qty * fiyat * fee
            cash += p.margin + raw - cikis_fee - p.funding
            pnl = p.realize_kismi + raw - cikis_fee - p.funding - p.entry_fee
        islemler.append({"symbol": s, "side": p.side, "pnl": pnl,
                         "reason": reason, "time": t})
        gun_realize += pnl - p.realize_kismi   # kısmiler zaten sayılmıştı
        del poz[s]

    for t in zamanlar:
        # ---------- gün sınırı ----------
        gun = pd.Timestamp(t, unit="ms", tz="UTC").date()
        if gun != su_anki_gun:
            su_anki_gun = gun
            gun_bas_equity = equity_hesapla(t)
            gun_realize = 0.0

        # ---------- açık pozisyonları yönet ----------
        for s in list(poz.keys()):
            d = veri[s]
            if t not in d.index:
                continue
            p = poz[s]
            o = float(d.at[t, "open"]); h = float(d.at[t, "high"])
            lo = float(d.at[t, "low"]); atr = float(d.at[t, "atr"])
            p.funding += p.qty * p.entry * fund[p.side] * (bar_hours / 24.0)

            liq_move = LIQ_BUFFER / leverage
            if p.side == "LONG":
                liq = p.entry * (1 - liq_move)
                if leverage > 1 and lo <= liq:
                    kapat(s, liq, "liq", t); continue
                if o <= p.stop:
                    kapat(s, o * (1 - SLIPPAGE), "stop", t); continue
                if lo <= p.stop:
                    kapat(s, p.stop * (1 - SLIPPAGE), "stop", t); continue
            else:
                liq = p.entry * (1 + liq_move)
                if leverage > 1 and h >= liq:
                    kapat(s, liq, "liq", t); continue
                if o >= p.stop:
                    kapat(s, o * (1 + SLIPPAGE), "stop", t); continue
                if h >= p.stop:
                    kapat(s, p.stop * (1 + SLIPPAGE), "stop", t); continue

            # sert kâr hedefi — stop kontrolünden SONRA (karamsar sıralama:
            # aynı barda ikisi de mümkünse stop çalışmış sayılır)
            if tam_cikis_r and p.risk_birimi > 0:
                hedef = (p.entry + tam_cikis_r * p.risk_birimi if p.side == "LONG"
                         else p.entry - tam_cikis_r * p.risk_birimi)
                ulasti = h >= hedef if p.side == "LONG" else lo <= hedef
                if ulasti:
                    slip = (1 - SLIPPAGE) if p.side == "LONG" else (1 + SLIPPAGE)
                    kapat(s, hedef * slip, "kâr hedefi", t)
                    continue

            # kısmi çıkışlar ("yarısını al, kalanı devam etsin")
            if kismi_hedefler and p.risk_birimi > 0:
                for idx, (r_sev, oran) in enumerate(kismi_hedefler):
                    if idx in p.kismi_bitti:
                        continue
                    hedef = (p.entry + r_sev * p.risk_birimi if p.side == "LONG"
                             else p.entry - r_sev * p.risk_birimi)
                    ulasti = h >= hedef if p.side == "LONG" else lo <= hedef
                    if not ulasti:
                        continue
                    kapat_kismi(s, oran, hedef)
                    p.kismi_bitti.add(idx)
                    # kalan için stop en azından başabaşa çekilir (kullanıcının
                    # "stop'u yukarı al, kârdan devam et" dediği davranış)
                    if kismi_sonrasi_basabas:
                        p.stop = (max(p.stop, p.entry) if p.side == "LONG"
                                  else min(p.stop, p.entry))
                if p.qty <= 1e-12:
                    kapat(s, float(d.at[t, "close"]), "kısmi tamam", t)
                    continue

            if not np.isnan(atr):
                if p.side == "LONG":
                    p.extreme = max(p.extreme, h)
                    p.stop = max(p.stop, p.extreme - atr * params.atr_multiplier)
                else:
                    p.extreme = min(p.extreme, lo)
                    p.stop = min(p.stop, p.extreme + atr * params.atr_multiplier)

        # ---------- devre kesici (portföy geneli, canlı botla aynı) ----------
        girisler_serbest = True
        if gun_bas_equity > 0 and gun_realize <= -max_daily_loss_pct * gun_bas_equity:
            girisler_serbest = False
            devre_kesici_gunler.add(gun)
            # seçici: yalnızca ZARARDAKİ pozisyonlar kapatılır
            for s in list(poz.keys()):
                d = veri[s]
                if t not in d.index:
                    continue
                p = poz[s]
                c = float(d.at[t, "close"])
                upnl = (p.qty * (c - p.entry) if p.side == "LONG"
                        else p.qty * (p.entry - c)) - p.funding
                if upnl < 0:
                    slip = (1 - SLIPPAGE) if p.side == "LONG" else (1 + SLIPPAGE)
                    kapat(s, c * slip, "günlük sermaye stopu", t)

        # ---------- girişler ----------
        if girisler_serbest:
            for s in semboller:
                if s in poz:
                    continue
                d = veri[s]
                if t not in d.index:
                    continue
                i = d.index.get_loc(t)
                if i == 0 or i < params.warmup_bars:
                    continue
                prev = d.iloc[i - 1]
                sig_l = check_entry(prev, params)
                sig_s = check_entry_short(prev, params)
                yon = "LONG" if sig_l.should_enter else ("SHORT" if sig_s.should_enter else "")
                if not yon:
                    continue
                if use_analyzer:
                    du = bool(prev["daily_uptrend"]) if "daily_uptrend" in prev.index else None
                    if not analyzer_allows(assess(s, prev, params, du), yon):
                        continue

                # --- koruma tavanları ---
                if max_positions is not None and len(poz) >= max_positions:
                    continue
                if max_ayni_yon is not None:
                    ayni = sum(1 for p in poz.values() if p.side == yon)
                    if ayni >= max_ayni_yon:
                        continue

                sig = sig_l if yon == "LONG" else sig_s
                o = float(d.at[t, "open"])
                fill = o * (1 + SLIPPAGE) if yon == "LONG" else o * (1 - SLIPPAGE)
                stop_mesafe = sig.atr * params.atr_multiplier
                if not (stop_mesafe > 0):
                    continue
                q = min((cash * risk_pct) / stop_mesafe,
                        (cash * max_balance_usage * leverage) / fill)
                notional = q * fill
                m = notional / leverage
                if q <= 0 or notional < 10 or m > cash * max_balance_usage:
                    continue
                entry_fee = notional * fee
                cash -= m + entry_fee
                poz[s] = Poz(yon, q, fill, m,
                             fill - stop_mesafe if yon == "LONG" else fill + stop_mesafe,
                             fill, 0.0, stop_mesafe, entry_fee, orig_qty=q)

        # ---------- ölçüm ----------
        n = len(poz)
        es_zamanli_kayit.append(n)
        max_es = max(max_es, n)
        if poz:
            l = sum(1 for p in poz.values() if p.side == "LONG")
            max_yon = max(max_yon, l, n - l)
        eq_kayit.append(equity_hesapla(t))

    # kapanış
    son_t = zamanlar[-1]
    for s in list(poz.keys()):
        d = veri[s]
        c = float(d.at[son_t, "close"]) if son_t in d.index else d["close"].iloc[-1]
        p = poz[s]
        kapat(s, c * ((1 - SLIPPAGE) if p.side == "LONG" else (1 + SLIPPAGE)), "eod", son_t)

    return PortfoySonuc(ad, start_cash, cash, islemler, pd.Series(eq_kayit),
                        max_es, float(np.mean(es_zamanli_kayit)), max_yon,
                        len(devre_kesici_gunler))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limitler", action="store_true", help="koruma tavanlarını karşılaştır")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-01-01")
    args = ap.parse_args()

    P = CONFIG.strategy
    from src.analysis import SCORE_THRESHOLD
    print(f"PORTFÖY testi — tek bakiye, {len(CANLI_PARITELER)} parite")
    print(f"Ayar: Donchian={P.donchian_period} ATR×{P.atr_multiplier} "
          f"{CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f} eşik={SCORE_THRESHOLD} "
          f"günlük stop=%{CONFIG.max_daily_loss_pct*100:.0f}\n")

    veri = veri_hazirla(CANLI_PARITELER, P, args.start, args.end)

    # ---- korelasyon ----
    ret = pd.DataFrame({s: d.set_index("open_time")["close"].pct_change()
                        for s, d in veri.items()}).dropna()
    korr = ret.corr()
    ust = korr.where(~np.eye(len(korr), dtype=bool))
    print("=" * 78)
    print("  KORELASYON — 4h getiriler (2022-2026)")
    print("=" * 78)
    print(f"  Ortalama ikili korelasyon : {ust.stack().mean():.3f}")
    print(f"  Medyan                    : {ust.stack().median():.3f}")
    print(f"  En düşük / en yüksek      : {ust.stack().min():.3f} / {ust.stack().max():.3f}")
    print(f"  BTC ile ortalama          : {korr['BTCUSDT'].drop('BTCUSDT').mean():.3f}")
    print("\n  Yorum: 1.00'a yakın = aynı anda aynı yöne hareket = çeşitlendirme yok.")

    senaryolar = [("Sınırsız (şu anki)", None, None)]
    if args.limitler:
        senaryolar += [
            ("En fazla 6 pozisyon", 6, None),
            ("En fazla 4 pozisyon", 4, None),
            ("En fazla 3 pozisyon", 3, None),
            ("Aynı yönde en fazla 4", None, 4),
            ("Aynı yönde en fazla 3", None, 3),
            ("6 poz + aynı yön 4", 6, 4),
        ]

    rows = []
    for ad, mp, my in senaryolar:
        r = run_portfoy(veri, P, ad=ad, leverage=CONFIG.leverage,
                        risk_pct=CONFIG.risk_pct,
                        max_daily_loss_pct=CONFIG.max_daily_loss_pct,
                        max_positions=mp, max_ayni_yon=my)
        rows.append(r.satir())

    print("\n" + "=" * 78)
    print("  PORTFÖY SONUÇLARI (tek bakiye $10.000)")
    print("=" * 78)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
