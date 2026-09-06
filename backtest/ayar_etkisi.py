"""Canlı ayarların her birinin getiriye etkisini TEK TEK ölçer.

Neden: doküman tablosu (BTC +%59.9) spot/Donchian20/risk%2 ile üretilmişti;
canlı bot ise futures/Donchian10/risk%1/2x/analiz-açık çalışıyor. İkisi
karşılaştırılamaz. Bu dosya canlı ayardan başlayıp her seferinde TEK bir
değişkeni değiştirir, böylece hangi tercihin ne kadara mal olduğu görünür.

    python -m backtest.ayar_etkisi
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.futures_engine import run_futures_backtest  # noqa: E402
from backtest.kar_cikis import CANLI_PARITELER  # noqa: E402

P = CONFIG.strategy

# (etiket, strateji parametre farkı, motor argüman farkı)
SENARYOLAR = [
    ("0) CANLI AYAR (Donchian 10)",      {}, {}),
    ("1) Donchian 15",                    {"donchian_period": 15}, {}),
    ("2) Donchian 20 (sabırlı)",          {"donchian_period": 20}, {}),
    ("3) Donchian 20 + risk %2",          {"donchian_period": 20}, {"risk_pct": 0.02}),
    ("4) Donchian 20, analiz KAPALI",     {"donchian_period": 20}, {"use_analyzer": False}),
    ("5) Donchian 20, sadece LONG",       {"donchian_period": 20}, {"allow_short": False}),
    ("6) Donchian 20 + risk %2 + LONG",   {"donchian_period": 20},
                                          {"risk_pct": 0.02, "allow_short": False}),
    ("7) Donchian 10, sadece LONG",       {}, {"allow_short": False}),
    ("8) Donchian 20, kaldıraç 1x",       {"donchian_period": 20}, {"leverage": 1.0}),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-01-01")
    args = ap.parse_args()

    semboller = [args.symbol] if args.symbol else CANLI_PARITELER
    print(f"Temel: kaldıraç={CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f} "
          f"ATR×{P.atr_multiplier} analiz=AÇIK long+short")
    print(f"Dönem: {args.start} → {args.end} (4h)\n")

    birikim: dict[str, list[dict]] = {s[0]: [] for s in SENARYOLAR}

    for sym in semboller:
        df = download_klines(sym, "4h", args.start, args.end)
        daily = download_klines(sym, "1d", "2021-02-15", "2026-01-01")
        satir = []
        for ad, p_fark, m_fark in SENARYOLAR:
            params = replace(P, **p_fark) if p_fark else P
            kw = {"leverage": CONFIG.leverage, "risk_pct": CONFIG.risk_pct,
                  "allow_long": True, "allow_short": True, "use_analyzer": True}
            kw.update(m_fark)
            r = run_futures_backtest(df, params, daily_df=daily, label={"Senaryo": ad}, **kw)
            s = r.summary_row()
            satir.append(s)
            birikim[ad].append(s)
        print(f"\n=== {sym} ===")
        print(pd.DataFrame(satir)[
            ["Senaryo", "Getiri %", "MaxDD %", "Sharpe", "İşlem",
             "Kazanma %", "PF", "Long PnL", "Short PnL", "Likid."]
        ].to_string(index=False))

    if len(semboller) > 1:
        print("\n\n" + "=" * 104)
        print(f"  {len(semboller)} PARİTE ORTALAMASI — hangi ayar ne kadara mal oluyor")
        print("=" * 104)
        ozet = []
        for ad, rows in birikim.items():
            pf = [r["PF"] for r in rows if isinstance(r["PF"], (int, float))]
            islem = np.mean([r["İşlem"] for r in rows])
            ozet.append({
                "Senaryo": ad,
                "Ort.Getiri %": round(np.mean([r["Getiri %"] for r in rows]), 1),
                "Medyan %": round(np.median([r["Getiri %"] for r in rows]), 1),
                "Pozitif": f"{sum(1 for r in rows if r['Getiri %'] > 0)}/{len(rows)}",
                "Ort.MaxDD %": round(np.mean([r["MaxDD %"] for r in rows]), 1),
                "Ort.Sharpe": round(np.mean([r["Sharpe"] for r in rows]), 2),
                "İşlem/yıl": round(islem / 4, 1),
                "Ort.Kazanma %": round(np.mean([r["Kazanma %"] for r in rows]), 1),
                "Ort.PF": round(np.mean(pf), 2) if pf else 0,
                "Short PnL": round(np.mean([r["Short PnL"] for r in rows]), 0),
                "Likid.": sum(r["Likid."] for r in rows),
            })
        print(pd.DataFrame(ozet).to_string(index=False))


if __name__ == "__main__":
    main()
