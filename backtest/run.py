"""Backtest / optimizasyon komut satırı aracı.

Örnekler:
    # Tek backtest (varsayılan parametrelerle)
    python -m backtest.run --symbol BTCUSDT --start 2023-01-01 --end 2026-01-01

    # ATR çarpanı + Donchian penceresi taraması ve out-of-sample doğrulama
    python -m backtest.run --symbol BTCUSDT --start 2022-01-01 --end 2026-01-01 --optimize

Out-of-sample mantığı: --optimize verildiğinde aralık %70 eğitim / %30 test
olarak bölünür. Parametreler yalnızca eğitim dilimiyle seçilir, ardından
test diliminde doğrulanır. Test sonucu kötüyse strateji overfit demektir.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1254 konsolu için

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import StrategyParams  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.engine import run_backtest  # noqa: E402

BARS_PER_YEAR = {
    "1m": 525_600, "5m": 105_120, "15m": 35_040, "30m": 17_520,
    "1h": 8_760, "4h": 2_190, "1d": 365,
}


def single(df: pd.DataFrame, params: StrategyParams, tag: str = "", bpy: float = 8760.0) -> None:
    res = run_backtest(df, params, bars_per_year=bpy)
    row = res.summary_row()
    print(f"\n=== SONUÇ {tag} ===")
    for k, v in row.items():
        print(f"  {k:12}: {v}")


def optimize(df: pd.DataFrame, base: StrategyParams, bpy: float = 8760.0) -> None:
    split = int(len(df) * 0.7)
    train, test = df.iloc[:split].reset_index(drop=True), df.iloc[split - base.warmup_bars:].reset_index(drop=True)
    print(f"Eğitim: {split} bar | Test: {len(df) - split} bar (out-of-sample)")

    rows = []
    for atr_mult in (1.5, 2.0, 2.5, 3.0, 3.5):
        for donchian in (10, 20, 40):
            p = replace(base, atr_multiplier=atr_mult, donchian_period=donchian)
            res = run_backtest(train, p, label={"ATR×": atr_mult, "Donchian": donchian},
                               bars_per_year=bpy)
            rows.append((res.summary_row(), p))

    table = pd.DataFrame([r for r, _ in rows]).sort_values("Sharpe", ascending=False)
    print("\n=== EĞİTİM DİLİMİ TARAMASI (Sharpe'a göre sıralı) ===")
    print(table.to_string(index=False))

    # Overfitting'e karşı: tek zirve yerine sağlam bölge — en iyi 3'ün ortalama parametresi değil,
    # en yüksek Sharpe'lı kombinasyonu al ama test diliminde DOĞRULA.
    best_row, best_params = max(rows, key=lambda rp: rp[0]["Sharpe"])
    print(f"\nSeçilen parametreler (eğitim): ATR×{best_params.atr_multiplier}, "
          f"Donchian={best_params.donchian_period}")

    res_test = run_backtest(test, best_params,
                            label={"ATR×": best_params.atr_multiplier,
                                   "Donchian": best_params.donchian_period},
                            bars_per_year=bpy)
    print("\n=== OUT-OF-SAMPLE (TEST DİLİMİ) DOĞRULAMASI ===")
    for k, v in res_test.summary_row().items():
        print(f"  {k:12}: {v}")
    if res_test.total_return_pct <= 0:
        print("\n⚠️  UYARI: Seçilen parametreler test diliminde para KAYBETTİ.")
        print("   Bu strateji bu haliyle canlıya alınmamalı (overfitting işareti).")
    else:
        print("\n✓ Test diliminde de pozitif — parametreler .env dosyasına yazılabilir:")
        print(f"   ATR_MULTIPLIER={best_params.atr_multiplier}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--optimize", action="store_true", help="parametre taraması + OOS testi")
    ap.add_argument("--atr-mult", type=float, default=None)
    ap.add_argument("--donchian", type=int, default=None)
    args = ap.parse_args()

    df = download_klines(args.symbol, args.interval, args.start, args.end)
    params = StrategyParams()
    if args.atr_mult is not None:
        params = replace(params, atr_multiplier=args.atr_mult)
    if args.donchian is not None:
        params = replace(params, donchian_period=args.donchian)

    bpy = BARS_PER_YEAR.get(args.interval, 8760.0)
    if args.optimize:
        optimize(df, params, bpy)
    else:
        single(df, params, f"{args.symbol} {args.interval} {args.start}→{args.end}", bpy)


if __name__ == "__main__":
    main()
