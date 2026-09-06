"""Sert kâr hedefinin SAĞLAMLIK testi — tek bir iyi sonuca kanmamak için.

10 şema denenip en iyisi seçilirse, kazanan büyük ihtimalle gürültüdür.
Bir bulgunun gerçek olduğuna ancak şu üç testi de geçerse inanılır:

  1) PLATO testi  — komşu R seviyeleri de iyi mi? Tek bir R'de zirve varsa
                    bu eğri uydurmadır; geniş bir plato varsa gerçek etkidir.
  2) PARİTE testi — kaç paritede tek tek baz senaryoyu yeniyor? 10/10 iyi,
                    6/10 tesadüf.
  3) DÖNEM testi  — dönemin ilk yarısında ve ikinci yarısında AYRI AYRI
                    kazanıyor mu? Sadece birinde kazanıyorsa güvenilmez.

    python -m backtest.kar_hedefi_saglamlik
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.kar_cikis import CANLI_PARITELER, CikisSemasi, calistir  # noqa: E402

P = CONFIG.strategy
R_SEVIYELERI = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]


def kosu(df, daily, tam_cikis_r):
    sema = CikisSemasi("x", tam_cikis_r=tam_cikis_r)
    return calistir(df, sema, P, leverage=CONFIG.leverage,
                    risk_pct=CONFIG.risk_pct, daily_df=daily)


def main() -> None:
    print("Sert kâr hedefi — SAĞLAMLIK TESTİ")
    print(f"Canlı ayar: Donchian={P.donchian_period} ATR×{P.atr_multiplier} "
          f"{CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f}\n")

    veri = {}
    for sym in CANLI_PARITELER:
        veri[sym] = (download_klines(sym, "4h", "2022-01-01", "2026-01-01"),
                     download_klines(sym, "1d", "2021-02-15", "2026-01-01"))

    # ---------------- 1) PLATO ----------------
    print("\n" + "=" * 92)
    print("  TEST 1 — PLATO: komşu R seviyeleri de iyi mi?")
    print("=" * 92)
    plato_getiri: dict[float | None, list[float]] = {}
    for r in [None] + R_SEVIYELERI:
        getiriler = []
        for sym, (df, daily) in veri.items():
            getiriler.append(kosu(df, daily, r).getiri_pct)
        plato_getiri[r] = getiriler

    baz = plato_getiri[None]
    satir = []
    for r, g in plato_getiri.items():
        satir.append({
            "Kâr hedefi": "YOK (mevcut)" if r is None else f"{r:.0f}R",
            "Ort.Getiri %": round(np.mean(g), 1),
            "Medyan %": round(np.median(g), 1),
            "Pozitif": f"{sum(1 for x in g if x > 0)}/{len(g)}",
            "Bazı yenen": "—" if r is None else f"{sum(1 for a, b in zip(g, baz) if a > b)}/{len(g)}",
            "Ort.fark pp": "—" if r is None else round(np.mean(g) - np.mean(baz), 2),
        })
    print(pd.DataFrame(satir).to_string(index=False))

    # ---------------- 2) PARİTE KIRILIMI (5R) ----------------
    print("\n" + "=" * 92)
    print("  TEST 2 — PARİTE KIRILIMI: 5R hedefi her paritede mi kazanıyor?")
    print("=" * 92)
    kirilim = []
    for i, sym in enumerate(CANLI_PARITELER):
        kirilim.append({
            "Parite": sym,
            "Hedefsiz %": round(baz[i], 1),
            "5R hedefli %": round(plato_getiri[5.0][i], 1),
            "Fark pp": round(plato_getiri[5.0][i] - baz[i], 1),
            "Sonuç": "5R iyi" if plato_getiri[5.0][i] > baz[i] else "hedefsiz iyi",
        })
    print(pd.DataFrame(kirilim).to_string(index=False))

    # ---------------- 3) DÖNEM ----------------
    print("\n" + "=" * 92)
    print("  TEST 3 — DÖNEM: ilk yarı (2022-23) ve ikinci yarı (2024-25) ayrı ayrı")
    print("=" * 92)
    donem_satir = []
    for ad, bas, bit in (("İlk yarı 2022-2024", "2022-01-01", "2024-01-01"),
                         ("İkinci yarı 2024-2026", "2024-01-01", "2026-01-01")):
        hedefsiz, h5, h3 = [], [], []
        for sym in CANLI_PARITELER:
            df = download_klines(sym, "4h", bas, bit)
            daily = download_klines(sym, "1d", "2021-02-15", "2026-01-01")
            hedefsiz.append(kosu(df, daily, None).getiri_pct)
            h3.append(kosu(df, daily, 3.0).getiri_pct)
            h5.append(kosu(df, daily, 5.0).getiri_pct)
        donem_satir.append({
            "Dönem": ad,
            "Hedefsiz ort %": round(np.mean(hedefsiz), 1),
            "3R ort %": round(np.mean(h3), 1),
            "5R ort %": round(np.mean(h5), 1),
            "3R bazı yendi": f"{sum(1 for a, b in zip(h3, hedefsiz) if a > b)}/10",
            "5R bazı yendi": f"{sum(1 for a, b in zip(h5, hedefsiz) if a > b)}/10",
        })
    print(pd.DataFrame(donem_satir).to_string(index=False))

    print("\n" + "=" * 92)
    print("  KARAR KURALI: bir şemayı ancak ÜÇ testi de geçerse benimseriz —")
    print("  geniş plato + ≥7/10 parite + her iki dönemde de üstünlük.")
    print("=" * 92)


if __name__ == "__main__":
    main()
