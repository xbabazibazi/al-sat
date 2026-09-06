"""28 varlığı GÜNCEL ayarla yeniden tarar.

NEDEN GEREKLİ: mevcut 10 parite listesi, analiz eşiği 50 iken yapılan bir
taramayla seçilmişti. Eşik 50'nin günlük EMA filtresini gizlice dayattığı
(ve getiriyi ~%40 kırptığı) sonradan kanıtlandı. Yani elenen 17 varlık
"kötü oldukları için" değil, "bozuk konfigürasyonda ölçüldükleri için"
elenmiş olabilir. Bu tarama o şüpheyi giderir.

Seçim kriteri sadece getiri DEĞİL — tek bir varlıkta yüksek getiri şans
olabilir. Sharpe (risk başına getiri) ve PF birlikte bakılır.

    python -m backtest.varlik_tarama
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import SCORE_THRESHOLD  # noqa: E402
from src.config import CONFIG, DATA_DIR  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.futures_engine import run_futures_backtest  # noqa: E402
from backtest.kar_cikis import CANLI_PARITELER  # noqa: E402


def mevcut_semboller() -> list[str]:
    """data/ altında hem 4h hem 1d verisi olan semboller."""
    dortsaat, gunluk = set(), set()
    for p in DATA_DIR.glob("*_4h_2022-01-01_2026-01-01.csv"):
        m = re.match(r"([A-Z]+)_4h_", p.name)
        if m:
            dortsaat.add(m.group(1))
    for p in DATA_DIR.glob("*_1d_*.csv"):
        m = re.match(r"([A-Z]+)_1d_", p.name)
        if m:
            gunluk.add(m.group(1))
    return sorted(dortsaat & gunluk)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-01-01")
    args = ap.parse_args()

    P = CONFIG.strategy
    semboller = mevcut_semboller()
    print(f"GÜNCEL AYARLA VARLIK TARAMASI — {len(semboller)} varlık")
    print(f"Ayar: Donchian={P.donchian_period} ATR×{P.atr_multiplier} "
          f"{CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f} eşik={SCORE_THRESHOLD} long+short")
    print(f"Dönem: {args.start} → {args.end} (4h)\n")

    rows = []
    for s in semboller:
        try:
            df = download_klines(s, "4h", args.start, args.end)
            d1 = download_klines(s, "1d", "2021-02-15", "2026-01-01")
            r = run_futures_backtest(df, P, leverage=CONFIG.leverage,
                                     risk_pct=CONFIG.risk_pct, daily_df=d1,
                                     use_analyzer=True)
            x = r.summary_row()
            rows.append({
                "Varlık": s.replace("USDT", ""),
                "Getiri %": x["Getiri %"],
                "MaxDD %": x["MaxDD %"],
                "Sharpe": x["Sharpe"],
                "İşlem": x["İşlem"],
                "Kazanma %": x["Kazanma %"],
                "PF": x["PF"] if isinstance(x["PF"], (int, float)) else 99.0,
                "Bar": len(df),
                "Listede": "✔" if s in CANLI_PARITELER else "",
            })
        except Exception as e:
            print(f"  {s}: atlandı ({e})")

    df = pd.DataFrame(rows).sort_values("Sharpe", ascending=False)
    print("=" * 96)
    print("  TÜM VARLIKLAR — Sharpe'a göre sıralı")
    print("=" * 96)
    print(df.to_string(index=False))

    # --- seçim kriteri ---
    kriter = (df["Sharpe"] >= 0.30) & (df["Getiri %"] > 0) & (df["PF"] >= 1.10)
    secilen = df[kriter]
    print("\n" + "=" * 96)
    print("  KRİTER: Sharpe ≥ 0.30 VE getiri > 0 VE PF ≥ 1.10")
    print("=" * 96)
    print(f"  Geçen varlık sayısı : {len(secilen)}")
    print(f"  Şu anki listeden geçen : {sum(1 for v in secilen['Varlık'] if v + 'USDT' in CANLI_PARITELER)}/10")

    yeni = [v for v in secilen["Varlık"] if v + "USDT" not in CANLI_PARITELER]
    dusen = [v.replace("USDT", "") for v in CANLI_PARITELER
             if v.replace("USDT", "") not in set(secilen["Varlık"])]
    print(f"\n  LİSTEYE GİRMEYİ HAK EDEN (şu an yok) : {', '.join(yeni) if yeni else '—'}")
    print(f"  LİSTEDE AMA KRİTERİ GEÇEMEYEN        : {', '.join(dusen) if dusen else '—'}")

    print("\n  NOT: Bu tarama her varlığı AYRI bakiyeyle ölçer. Portföyde")
    print("  korelasyon nedeniyle varlık EKLEMEK çeşitlendirme sağlamayabilir —")
    print("  portfoy.py ile birlikte değerlendirilmeli.")


if __name__ == "__main__":
    main()
