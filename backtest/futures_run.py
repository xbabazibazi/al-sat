"""Vadeli işlem senaryolarını test eder: yön (long/short/ikisi) × kaldıraç.

    python -m backtest.futures_run --symbol BTCUSDT
    python -m backtest.futures_run --all          # 3 parite tam matris
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import StrategyParams  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.futures_engine import run_futures_backtest  # noqa: E402


def matrix(symbol: str, start: str, end: str, interval: str = "4h") -> None:
    df = download_klines(symbol, interval, start, end)
    params = StrategyParams()
    bpy = 2190 if interval == "4h" else 8760
    bar_h = 4 if interval == "4h" else 1

    rows = []
    for direction, al, as_ in (("Sadece LONG", True, False),
                               ("Sadece SHORT", False, True),
                               ("LONG + SHORT", True, True)):
        for lev in (1, 2, 3):
            res = run_futures_backtest(
                df, params, leverage=lev, allow_long=al, allow_short=as_,
                label={"Yön": direction, "Kald.": f"{lev}x"},
                bar_hours=bar_h, bars_per_year=bpy,
            )
            rows.append(res.summary_row())
    print(f"\n=== {symbol} {interval} {start}→{end} — VADELİ İŞLEM MATRİSİ ===")
    print(pd.DataFrame(rows).to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--all", action="store_true", help="BTC+ETH+SOL tam matris")
    args = ap.parse_args()

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"] if args.all else [args.symbol]
    for sym in symbols:
        matrix(sym, args.start, args.end, args.interval)


if __name__ == "__main__":
    main()
