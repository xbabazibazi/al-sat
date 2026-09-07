"""ADAY TARAMASI — Binance'teki TÜM işlem görebilir pariteleri izole ölçer.

`varlik_tarama.py` yalnızca data/ altında zaten indirilmiş varlıklara bakar.
Bu dosya evreni borsadan CANLI çıkarır, yani "acaba hiç bakmadığımız bir coin
var mı" sorusunu kapatır.

Evren üç şartla daraltılır — üçü de keyfi değil, gerçeklik şartı:
  1) USDT-M KALICI VADELİ listesinde olmalı — bot yalnızca orada pozisyon açar.
  2) SPOT'ta da olmalı — mum verisi spot API'den geliyor (backtest/data.py).
  3) 24s hacim eşiği — likit olmayan pariteyi backtest'te alıp satmak yalandır;
     gerçek hayatta kayma (slippage) modellediğimizden büyük olur.
Ayrıca ~2 yıldan az geçmişi olanlar elenir (dönem yarıları testi yapılamaz).

!! BU TARAMA TEK BAŞINA KARAR VERDİRMEZ. Her varlığı AYRI bakiyeyle ölçer.
   Portföyde 10 paritenin ortalama korelasyonu 0.68; "tek başına iyi" varlık
   eklemek çeşitlendirme değil, aynı bahsi bir yerden daha oynamak olabilir.
   Kararı `marjinal_katki.py` verir. Buranın işi evreni oraya sokulabilecek
   boyuta indirmektir.

    python -m backtest.aday_tarama
    python -m backtest.aday_tarama --hacim 50      # daha sıkı likidite
    python -m backtest.aday_tarama --min-bar 8000  # yalnızca tam geçmişliler
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import SCORE_THRESHOLD  # noqa: E402
from src.config import CONFIG, DATA_DIR  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.futures_engine import run_futures_backtest  # noqa: E402
from backtest.kar_cikis import CANLI_PARITELER  # noqa: E402

FUT_API = "https://fapi.binance.com"
SPOT_API = "https://api.binance.com"
SONUC = DATA_DIR / "aday_tarama.csv"


def evren_cikar(min_hacim_musd: float) -> dict[str, float]:
    """Vadeli ∩ spot ∩ likidite. Dönen: {sembol: 24s hacim (milyon USD)}."""
    s = requests.Session()

    fut = s.get(f"{FUT_API}/fapi/v1/exchangeInfo", timeout=30).json()
    # 1000-önekliler (1000PEPE gibi) atlanır: spot adı farklı, fiyat ölçeği farklı.
    vadeli = {x["symbol"] for x in fut["symbols"]
              if x["status"] == "TRADING" and x["contractType"] == "PERPETUAL"
              and x["quoteAsset"] == "USDT" and not x["symbol"].startswith("1000")}

    spot = s.get(f"{SPOT_API}/api/v3/exchangeInfo", timeout=30).json()
    spot_sym = {x["symbol"] for x in spot["symbols"] if x["status"] == "TRADING"}

    tick = s.get(f"{FUT_API}/fapi/v1/ticker/24hr", timeout=30).json()
    hacim = {t["symbol"]: float(t["quoteVolume"]) / 1e6 for t in tick}

    print(f"USDT-M kalıcı vadeli (TRADING) : {len(vadeli)}")
    print(f"Spot verisi de olan            : {len(vadeli & spot_sym)}")
    secim = {x: hacim.get(x, 0.0) for x in (vadeli & spot_sym)
             if hacim.get(x, 0.0) >= min_hacim_musd}
    # Canlı liste, hacim eşiğine takılsa bile daima dahil — karşılaştırma tabanı.
    for x in CANLI_PARITELER:
        secim.setdefault(x, hacim.get(x, 0.0))
    print(f"24s hacim ≥ ${min_hacim_musd:.0f}M (+ canlı liste) : {len(secim)}\n")
    return dict(sorted(secim.items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hacim", type=float, default=20.0,
                    help="asgari 24s vadeli hacim, milyon USD (varsayılan 20)")
    ap.add_argument("--min-bar", type=int, default=4000,
                    help="asgari 4h bar sayısı; 4000 ≈ 2 yıl (varsayılan 4000)")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-01-01")
    args = ap.parse_args()

    P = CONFIG.strategy
    evren = evren_cikar(args.hacim)
    print(f"Ayar: Donchian={P.donchian_period} ATR×{P.atr_multiplier} "
          f"{CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f} eşik={SCORE_THRESHOLD} "
          f"long+short")
    print(f"Dönem: {args.start} → {args.end} (4h)\n")

    rows, atlanan = [], []
    for i, (sym, hcm) in enumerate(evren.items(), 1):
        print(f"\r  [{i}/{len(evren)}] {sym:<14s} ölçülen={len(rows)} "
              f"atlanan={len(atlanan)}   ", end="", flush=True)
        try:
            df = download_klines(sym, "4h", args.start, args.end)
            if len(df) < args.min_bar:
                atlanan.append((sym, f"{len(df)} bar"))
                continue
            d1 = download_klines(sym, "1d", "2021-02-15", "2026-01-01")
            r = run_futures_backtest(df, P, leverage=CONFIG.leverage,
                                     risk_pct=CONFIG.risk_pct, daily_df=d1,
                                     use_analyzer=True)
            x = r.summary_row()
            rows.append({
                "Varlık": sym.replace("USDT", ""), "sembol": sym,
                "Getiri %": x["Getiri %"], "MaxDD %": x["MaxDD %"],
                "Sharpe": x["Sharpe"], "İşlem": x["İşlem"],
                "Kazanma %": x["Kazanma %"],
                "PF": x["PF"] if isinstance(x["PF"], (int, float)) else 99.0,
                "Bar": len(df), "Hacim $M": round(hcm),
                "Listede": "✔" if sym in CANLI_PARITELER else "",
            })
        except Exception as e:  # ağ/veri hatası bir varlığı düşürsün, taramayı değil
            atlanan.append((sym, str(e)[:40]))

    print("\n")
    df = pd.DataFrame(rows).sort_values("Sharpe", ascending=False)
    df.to_csv(SONUC, index=False)

    print("=" * 104)
    print("  İZOLE TARAMA — Sharpe'a göre sıralı   (✔ = şu anki canlı listede)")
    print("=" * 104)
    print(df.drop(columns=["sembol"]).to_string(index=False))

    kriter = (df["Sharpe"] >= 0.30) & (df["Getiri %"] > 0) & (df["PF"] >= 1.10)
    aday = [s for s in df[kriter]["sembol"] if s not in CANLI_PARITELER]
    print(f"\n  Kriter (Sharpe≥0.30, getiri>0, PF≥1.10) geçen : {int(kriter.sum())}")
    print(f"  Bunlardan canlı listede OLMAYAN              : {len(aday)}")
    print(f"    {', '.join(a.replace('USDT', '') for a in aday) or '—'}")
    if atlanan:
        print(f"\n  Atlanan ({len(atlanan)}): "
              + ", ".join(f"{a}({b})" for a, b in atlanan[:20])
              + (" …" if len(atlanan) > 20 else ""))
    print(f"\n  Kaydedildi: {SONUC}")
    print("\n  ⚠ SONRAKİ ADIM ZORUNLU: bu tablo AYRI bakiyeli ölçümdür ve tek başına")
    print("    karar verdirmez. Adayları portföyde sına:")
    print("      python -m backtest.marjinal_katki")


if __name__ == "__main__":
    main()
