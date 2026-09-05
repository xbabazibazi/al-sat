"""Backtest / optimizasyon / walk-forward komut satırı aracı.

Örnekler:
    # Tek backtest (günlük trend filtresi dahil)
    python -m backtest.run --symbol BTCUSDT --interval 4h --start 2022-01-01 --end 2026-01-01

    # İyileştirmelerin katkısını ayrı ayrı gösteren A/B tablosu
    python -m backtest.run --symbol BTCUSDT --interval 4h --start 2022-01-01 --end 2026-01-01 --compare

    # Parametre taraması + out-of-sample doğrulama
    python -m backtest.run ... --optimize

    # Walk-forward: 12 ay eğit → 6 ay test, 6 ay kaydır (en güvenilir doğrulama)
    python -m backtest.run ... --walkforward
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta
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

# Komisyon senaryoları (giriş, çıkış) — taraf başına oranlar
FEE_BASE = (0.001, 0.001)            # %0.10 taker / taker
FEE_BNB = (0.00075, 0.00075)         # BNB indirimi açık
FEE_BNB_MAKER = (0.0005625, 0.00075) # BNB + girişte maker limit dolumu


def load_daily(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Günlük filtre için 1d veri — EMA200 ısınması adına 320 gün erken başlar."""
    d_start = (datetime.fromisoformat(start) - timedelta(days=320)).date().isoformat()
    return download_klines(symbol, "1d", d_start, end)


def print_result(res, tag: str) -> None:
    print(f"\n=== SONUÇ {tag} ===")
    for k, v in res.summary_row().items():
        print(f"  {k:12}: {v}")


def compare(df: pd.DataFrame, daily_df: pd.DataFrame, params: StrategyParams, bpy: float) -> None:
    """İyileştirmelerin katkısını kademeli olarak gösterir."""
    scenarios = [
        ("Temel (filtre yok, %0.10)", replace(params, use_daily_filter=False), FEE_BASE, None),
        ("+ Günlük trend filtresi", replace(params, use_daily_filter=True), FEE_BASE, daily_df),
        ("+ BNB komisyon indirimi", replace(params, use_daily_filter=True), FEE_BNB, daily_df),
        ("+ Maker limit girişi", replace(params, use_daily_filter=True), FEE_BNB_MAKER, daily_df),
    ]
    rows = []
    for name, p, (ef, xf), ddf in scenarios:
        res = run_backtest(df, p, label={"Senaryo": name}, bars_per_year=bpy,
                           entry_fee=ef, exit_fee=xf, daily_df=ddf)
        rows.append(res.summary_row())
    print("\n=== İYİLEŞTİRME KATKI TABLOSU (kademeli) ===")
    print(pd.DataFrame(rows).to_string(index=False))


def optimize(df: pd.DataFrame, daily_df: pd.DataFrame | None, base: StrategyParams,
             bpy: float, fees: tuple[float, float] = FEE_BNB) -> None:
    split = int(len(df) * 0.7)
    train = df.iloc[:split].reset_index(drop=True)
    test = df.iloc[split - base.warmup_bars:].reset_index(drop=True)
    print(f"Eğitim: {split} bar | Test: {len(df) - split} bar (out-of-sample)")

    rows = []
    for atr_mult in (1.5, 2.0, 2.5, 3.0, 3.5):
        for donchian in (10, 20, 40):
            p = replace(base, atr_multiplier=atr_mult, donchian_period=donchian)
            res = run_backtest(train, p, label={"ATR×": atr_mult, "Donchian": donchian},
                               bars_per_year=bpy, entry_fee=fees[0], exit_fee=fees[1],
                               daily_df=daily_df)
            rows.append((res.summary_row(), p))

    table = pd.DataFrame([r for r, _ in rows]).sort_values("Sharpe", ascending=False)
    print("\n=== EĞİTİM DİLİMİ TARAMASI (Sharpe'a göre sıralı) ===")
    print(table.to_string(index=False))

    best_row, best_params = max(rows, key=lambda rp: rp[0]["Sharpe"])
    print(f"\nSeçilen parametreler (eğitim): ATR×{best_params.atr_multiplier}, "
          f"Donchian={best_params.donchian_period}")

    res_test = run_backtest(test, best_params,
                            label={"ATR×": best_params.atr_multiplier,
                                   "Donchian": best_params.donchian_period},
                            bars_per_year=bpy, entry_fee=fees[0], exit_fee=fees[1],
                            daily_df=daily_df)
    print_result(res_test, "OUT-OF-SAMPLE (TEST DİLİMİ)")
    if res_test.total_return_pct <= 0:
        print("\n⚠️  UYARI: Seçilen parametreler test diliminde para KAYBETTİ (overfitting işareti).")
    else:
        print(f"\n✓ Test diliminde de pozitif. .env için: ATR_MULTIPLIER={best_params.atr_multiplier}")


def walkforward(df: pd.DataFrame, daily_df: pd.DataFrame | None, base: StrategyParams,
                bpy: float, train_months: int = 12, test_months: int = 6,
                fees: tuple[float, float] = FEE_BNB) -> None:
    """Kayan pencereli doğrulama — tek bölmeli optimizasyondan çok daha güvenilir.

    Her pencerede: eğitim diliminde en iyi (ATR×, Donchian) seçilir, hemen
    sonrasındaki test diliminde uygulanır. Rapor:
      - pencere pencere out-of-sample sonuçlar (gerçekte yaşanacak olan buydu)
      - seçilen parametrelerin kararlılığı (pencereden pencereye zıplıyorsa
        strateji parametreye aşırı duyarlı demektir → güvensiz)
    """
    bars_per_month = int(bpy / 12)
    train_bars = train_months * bars_per_month
    test_bars = test_months * bars_per_month
    grid = [(a, d) for a in (2.0, 2.5, 3.0, 3.5) for d in (10, 20, 40)]

    window_rows = []
    oos_compound = 1.0
    start = 0
    w = 0
    while start + train_bars + test_bars <= len(df):
        w += 1
        train = df.iloc[start:start + train_bars].reset_index(drop=True)
        # test dilimi, indikatör ısınması için eğitimin son warmup_bars barını da içerir
        test = df.iloc[start + train_bars - base.warmup_bars:
                       start + train_bars + test_bars].reset_index(drop=True)

        best_sharpe, best_p = -99.0, None
        for a, d in grid:
            p = replace(base, atr_multiplier=a, donchian_period=d)
            r = run_backtest(train, p, bars_per_year=bpy,
                             entry_fee=fees[0], exit_fee=fees[1], daily_df=daily_df)
            if r.sharpe > best_sharpe:
                best_sharpe, best_p = r.sharpe, p

        rt = run_backtest(test, best_p, bars_per_year=bpy,
                          entry_fee=fees[0], exit_fee=fees[1], daily_df=daily_df)
        oos_ret = rt.total_return_pct
        oos_compound *= (1 + oos_ret / 100)
        window_rows.append({
            "Pencere": w,
            "Seçilen ATR×": best_p.atr_multiplier,
            "Seçilen Donchian": best_p.donchian_period,
            "Eğitim Sharpe": round(best_sharpe, 2),
            "OOS Getiri %": round(oos_ret, 1),
            "OOS MaxDD %": round(rt.max_drawdown_pct, 1),
            "OOS İşlem": len(rt.trades),
        })
        start += test_bars

    if not window_rows:
        print("Veri walk-forward için çok kısa (en az eğitim+test kadar bar gerekli).")
        return

    table = pd.DataFrame(window_rows)
    print(f"\n=== WALK-FORWARD ({train_months} ay eğit → {test_months} ay test) ===")
    print(table.to_string(index=False))

    total_oos = (oos_compound - 1) * 100
    pos_windows = sum(1 for r in window_rows if r["OOS Getiri %"] > 0)
    atr_values = {r["Seçilen ATR×"] for r in window_rows}
    don_values = {r["Seçilen Donchian"] for r in window_rows}
    print(f"\nBileşik OOS getiri: %{total_oos:.1f} "
          f"({len(window_rows)} pencerenin {pos_windows} tanesi pozitif)")
    if len(atr_values) <= 2 and len(don_values) <= 2:
        print("✓ Parametre seçimi pencereler arasında KARARLI — strateji parametreye aşırı duyarlı değil.")
    else:
        print("⚠️  Parametre seçimi pencereden pencereye zıplıyor — overfitting'e dikkat.")
    if total_oos <= 0:
        print("⚠️  Bileşik OOS getiri negatif: bu yapı canlıya alınmamalı.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--compare", action="store_true", help="iyileştirme katkı tablosu")
    ap.add_argument("--walkforward", action="store_true")
    ap.add_argument("--wf-train", type=int, default=12, help="walk-forward eğitim ayı")
    ap.add_argument("--wf-test", type=int, default=6, help="walk-forward test ayı")
    ap.add_argument("--no-daily-filter", action="store_true")
    ap.add_argument("--atr-mult", type=float, default=None)
    ap.add_argument("--donchian", type=int, default=None)
    args = ap.parse_args()

    df = download_klines(args.symbol, args.interval, args.start, args.end)
    daily_df = None if args.interval == "1d" else load_daily(args.symbol, args.start, args.end)

    params = StrategyParams()
    if args.no_daily_filter:
        params = replace(params, use_daily_filter=False)
    if args.atr_mult is not None:
        params = replace(params, atr_multiplier=args.atr_mult)
    if args.donchian is not None:
        params = replace(params, donchian_period=args.donchian)

    bpy = BARS_PER_YEAR.get(args.interval, 8760.0)
    if args.compare:
        compare(df, daily_df, params, bpy)
    elif args.optimize:
        optimize(df, daily_df if params.use_daily_filter else None, params, bpy)
    elif args.walkforward:
        walkforward(df, daily_df if params.use_daily_filter else None, params, bpy,
                    args.wf_train, args.wf_test)
    else:
        res = run_backtest(df, params, bars_per_year=bpy,
                           entry_fee=FEE_BNB[0], exit_fee=FEE_BNB[1],
                           daily_df=daily_df if params.use_daily_filter else None)
        print_result(res, f"{args.symbol} {args.interval} {args.start}→{args.end}")


if __name__ == "__main__":
    main()
