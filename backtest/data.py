"""Binance'ten geçmiş mum verisi indirici.

Yahoo Finance DEĞİL, gerçek Binance verisi kullanılır (sohbetteki örneklerin
aksine) — canlı botun göreceği veriyle birebir aynı kaynak. Veri data/
klasörüne CSV olarak önbelleklenir; tekrar indirme gerekmez.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DATA_DIR  # noqa: E402
from src.exchange import KLINE_COLUMNS, PROD_API  # noqa: E402

INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def _to_ms(date_str: str) -> int:
    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def download_klines(symbol: str, interval: str, start: str, end: str,
                    force: bool = False) -> pd.DataFrame:
    """[start, end) aralığındaki tüm mumları indirir (CSV önbellekli)."""
    cache = DATA_DIR / f"{symbol}_{interval}_{start}_{end}.csv"
    if cache.exists() and not force:
        df = pd.read_csv(cache)
        print(f"[önbellek] {cache.name}: {len(df)} bar")
        return df

    start_ms, end_ms = _to_ms(start), _to_ms(end)
    step = INTERVAL_MS[interval]
    session = requests.Session()
    rows: list = []
    cursor = start_ms

    while cursor < end_ms:
        resp = session.get(
            f"{PROD_API}/api/v3/klines",
            params={"symbol": symbol, "interval": interval,
                    "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + step
        print(f"\r{symbol} {interval}: {len(rows)} bar indirildi...", end="", flush=True)
        time.sleep(0.15)  # rate limit nezaketi

    print()
    df = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)
    df.to_csv(cache, index=False)
    print(f"[kaydedildi] {cache.name}: {len(df)} bar")
    return df
