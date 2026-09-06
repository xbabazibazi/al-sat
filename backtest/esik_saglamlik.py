"""Analiz eşiği değişikliğinin SAĞLAMLIK testi + mekanizma kanıtı.

Kâr hedefi bulgusunu üç testten geçemediği için reddettik. Aynı çıta burada
da uygulanır — eşiği düşürmek "iyi göründüğü" için değil, kanıtlandığı için
değişir.

  KANIT   — eşik 50 gerçekten günlük EMA filtresini mi dayatıyor?
            Falsifiye edilebilir test: eşik 50'de günlük filtreyi AÇIK/KAPALI
            çalıştır. İddiam doğruysa sonuç neredeyse DEĞİŞMEZ (çünkü analiz
            katmanı onu zaten uyguluyor). Düşük eşikte ise BELİRGİN değişir.
  TEST 1  — plato: komşu eşikler de iyi mi?
  TEST 2  — parite kırılımı: kaç paritede tek tek kazanıyor?
  TEST 3  — dönem: her iki yarıda da kazanıyor mu?

    python -m backtest.esik_saglamlik
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import analysis  # noqa: E402
from src.config import CONFIG  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.futures_engine import run_futures_backtest  # noqa: E402
from backtest.kar_cikis import CANLI_PARITELER  # noqa: E402

P = CONFIG.strategy
SU_ANKI = analysis.SCORE_THRESHOLD


def kos(df, daily, esik, gunluk_filtre=False):
    params = replace(P, use_daily_filter=True) if gunluk_filtre else P
    onceki = analysis.SCORE_THRESHOLD
    analysis.SCORE_THRESHOLD = esik
    try:
        return run_futures_backtest(df, params, leverage=CONFIG.leverage,
                                    risk_pct=CONFIG.risk_pct, daily_df=daily,
                                    use_analyzer=True)
    finally:
        analysis.SCORE_THRESHOLD = onceki


def main() -> None:
    veri = {}
    for sym in CANLI_PARITELER:
        veri[sym] = (download_klines(sym, "4h", "2022-01-01", "2026-01-01"),
                     download_klines(sym, "1d", "2021-02-15", "2026-01-01"))

    # ---------------- MEKANİZMA KANITI ----------------
    print("=" * 100)
    print("  KANIT — eşik 50, günlük EMA filtresini gizlice dayatıyor mu?")
    print("  İddia doğruysa: eşik 50'de günlük filtreyi açmak sonucu DEĞİŞTİRMEZ.")
    print("=" * 100)
    kanit = []
    for esik in (50, 20, 0):
        kapali = [kos(df, d, esik, False).total_return_pct for df, d in veri.values()]
        acik = [kos(df, d, esik, True).total_return_pct for df, d in veri.values()]
        ayni = sum(1 for a, b in zip(kapali, acik) if abs(a - b) < 0.05)
        kanit.append({
            "Eşik": esik,
            "Günlük filtre KAPALI %": round(np.mean(kapali), 1),
            "Günlük filtre AÇIK %": round(np.mean(acik), 1),
            "Fark pp": round(np.mean(acik) - np.mean(kapali), 2),
            "Değişmeyen parite": f"{ayni}/10",
        })
    print(pd.DataFrame(kanit).to_string(index=False))

    # ---------------- TEST 1: PLATO ----------------
    ESIKLER = [0, 10, 20, 25, 30, 40, 50, 60]
    print("\n" + "=" * 100)
    print("  TEST 1 — PLATO: düşük eşik bölgesi geniş mi, tek nokta mı?")
    print("=" * 100)
    plato = {}
    for e in ESIKLER:
        plato[e] = [kos(df, d, e).total_return_pct for df, d in veri.values()]
    baz = plato[SU_ANKI]
    print(pd.DataFrame([{
        "Eşik": f"{e}" + (" ← ŞU ANKİ" if e == SU_ANKI else ""),
        "Ort.Getiri %": round(np.mean(g), 1),
        "Medyan %": round(np.median(g), 1),
        "Pozitif": f"{sum(1 for x in g if x > 0)}/10",
        "Şu ankini yenen": "—" if e == SU_ANKI else f"{sum(1 for a, b in zip(g, baz) if a > b)}/10",
        "Ort.fark pp": "—" if e == SU_ANKI else round(np.mean(g) - np.mean(baz), 1),
    } for e, g in plato.items()]).to_string(index=False))

    # ---------------- TEST 2: PARİTE ----------------
    print("\n" + "=" * 100)
    print("  TEST 2 — PARİTE KIRILIMI: eşik 20 her paritede kazanıyor mu?")
    print("=" * 100)
    print(pd.DataFrame([{
        "Parite": sym,
        "Eşik 50 %": round(baz[i], 1),
        "Eşik 20 %": round(plato[20][i], 1),
        "Fark pp": round(plato[20][i] - baz[i], 1),
        "Sonuç": "20 iyi" if plato[20][i] > baz[i] else "50 iyi",
    } for i, sym in enumerate(CANLI_PARITELER)]).to_string(index=False))

    # ---------------- TEST 3: DÖNEM ----------------
    print("\n" + "=" * 100)
    print("  TEST 3 — DÖNEM: iki yarıda da üstün mü?")
    print("=" * 100)
    satir = []
    for ad, bas, bit in (("İlk yarı 2022-2024", "2022-01-01", "2024-01-01"),
                         ("İkinci yarı 2024-2026", "2024-01-01", "2026-01-01")):
        e50, e20, e0 = [], [], []
        for sym in CANLI_PARITELER:
            df = download_klines(sym, "4h", bas, bit)
            d = download_klines(sym, "1d", "2021-02-15", "2026-01-01")
            e50.append(kos(df, d, 50).total_return_pct)
            e20.append(kos(df, d, 20).total_return_pct)
            e0.append(kos(df, d, 0).total_return_pct)
        satir.append({
            "Dönem": ad,
            "Eşik 50 ort %": round(np.mean(e50), 1),
            "Eşik 20 ort %": round(np.mean(e20), 1),
            "Eşik 0 ort %": round(np.mean(e0), 1),
            "20 yendi": f"{sum(1 for a, b in zip(e20, e50) if a > b)}/10",
            "0 yendi": f"{sum(1 for a, b in zip(e0, e50) if a > b)}/10",
        })
    print(pd.DataFrame(satir).to_string(index=False))


if __name__ == "__main__":
    main()
