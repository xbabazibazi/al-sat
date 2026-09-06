"""Analiz eşiğinin (ANALYSIS_THRESHOLD) getiriye etkisini süpürür.

NEDEN ÖNEMLİ: giriş sinyali zaten fiyatın EMA200'ün doğru tarafında olmasını
şart koşuyor. Dolayısıyla "4s Trend" oyu bir LONG sinyalinde HER ZAMAN +30,
bir SHORT sinyalinde HER ZAMAN −30 gelir. Eşik 50 olduğunda bot ek olarak
±20 daha ister; bunu verebilecek tek araç günlük trend (±20) veya EMA eğimi
(±20). Yani eşik 50, A/B testinde tutarsız bulunup KAPATILAN günlük filtreyi
fiilen geri açar.

Bu süpürme, analiz katmanını KALDIRMADAN doğru eşiği bulmak içindir:
volatilite vetosu ve karşı-yön oylarının reddi korunur, gereksiz katılık gider.

    python -m backtest.analiz_esigi
"""
from __future__ import annotations

import sys
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

# None = analiz katmanı tamamen kapalı (referans; kullanıcı şartına AYKIRI,
# yalnızca eşiğin ne kadarına mal olduğunu görmek için ölçülür)
ESIKLER = [None, 0, 20, 30, 40, 50, 60]


def etiket(e) -> str:
    if e is None:
        return "analiz KAPALI (referans)"
    if e == 0:
        return "eşik 0 (veto + net yön)"
    return f"eşik {e}" + (" ← ŞU ANKİ" if e == analysis.SCORE_THRESHOLD else "")


def kos(df, daily, esik):
    if esik is None:
        return run_futures_backtest(df, P, leverage=CONFIG.leverage,
                                    risk_pct=CONFIG.risk_pct, daily_df=daily,
                                    use_analyzer=False)
    onceki = analysis.SCORE_THRESHOLD
    analysis.SCORE_THRESHOLD = esik
    try:
        return run_futures_backtest(df, P, leverage=CONFIG.leverage,
                                    risk_pct=CONFIG.risk_pct, daily_df=daily,
                                    use_analyzer=True)
    finally:
        analysis.SCORE_THRESHOLD = onceki


def main() -> None:
    su_anki = analysis.SCORE_THRESHOLD
    print(f"Analiz eşiği süpürmesi — şu anki değer: {su_anki}")
    print(f"Ayar: Donchian={P.donchian_period} ATR×{P.atr_multiplier} "
          f"{CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f} long+short")
    print("Dönem: 2022-01-01 → 2026-01-01 (4h), 10 parite\n")

    sonuc: dict[object, list[dict]] = {e: [] for e in ESIKLER}
    for sym in CANLI_PARITELER:
        df = download_klines(sym, "4h", "2022-01-01", "2026-01-01")
        daily = download_klines(sym, "1d", "2021-02-15", "2026-01-01")
        for e in ESIKLER:
            sonuc[e].append(kos(df, daily, e).summary_row())

    baz = [r["Getiri %"] for r in sonuc[su_anki]]
    ozet = []
    for e in ESIKLER:
        rows = sonuc[e]
        g = [r["Getiri %"] for r in rows]
        pf = [r["PF"] for r in rows if isinstance(r["PF"], (int, float))]
        ozet.append({
            "Analiz eşiği": etiket(e),
            "Ort.Getiri %": round(np.mean(g), 1),
            "Medyan %": round(np.median(g), 1),
            "Pozitif": f"{sum(1 for x in g if x > 0)}/{len(g)}",
            "Ort.MaxDD %": round(np.mean([r["MaxDD %"] for r in rows]), 1),
            "Ort.Sharpe": round(np.mean([r["Sharpe"] for r in rows]), 2),
            "İşlem/yıl": round(np.mean([r["İşlem"] for r in rows]) / 4, 1),
            "Kazanma %": round(np.mean([r["Kazanma %"] for r in rows]), 1),
            "Ort.PF": round(np.mean(pf), 2) if pf else 0,
            "Şu ankini yenen": "—" if e == su_anki else
                               f"{sum(1 for a, b in zip(g, baz) if a > b)}/{len(g)}",
        })
    print("=" * 118)
    print("  ANALİZ EŞİĞİ SÜPÜRMESİ")
    print("=" * 118)
    print(pd.DataFrame(ozet).to_string(index=False))

    print("\nNot: 'analiz KAPALI' satırı yalnızca ölçüm referansıdır — kullanıcı")
    print("şartı gereği analiz katmanı kaldırılmaz, eşiği kalibre edilir.")


if __name__ == "__main__":
    main()
