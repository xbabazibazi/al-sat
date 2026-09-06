"""Portföy KOMPOZİSYONU: hangi varlık listesi, hangi pozisyon tavanıyla?

Tek tek tarama "ADA iyi, BTC kötü" der. Ama portföyde soru bu değil:
korelasyon 0.68 olduğu için varlık EKLEMEK çeşitlendirme sağlamayabilir —
sadece aynı bahsi daha çok yerden oynamak olur. Bu dosya kompozisyonu
portföy düzeyinde, tek bakiyeyle ölçer.

DÜRÜSTLÜK NOTU: motor sembolleri liste sırasına göre tarar, dolayısıyla
pozisyon tavanı doluyken listenin BAŞINDAKİ varlıklar önceliklidir. Canlı
bot da .env sırasına göre çalıştığı için bu davranış gerçeği yansıtır, ama
sıralamanın sonuca etkisi ayrıca ölçülür (--sira testi).

    python -m backtest.portfoy_kompozisyon
    python -m backtest.portfoy_kompozisyon --sira    # sıralama duyarlılığı
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import SCORE_THRESHOLD  # noqa: E402
from src.config import CONFIG  # noqa: E402
from backtest.portfoy import run_portfoy, veri_hazirla  # noqa: E402

U = lambda xs: [x + "USDT" for x in xs]  # noqa: E731

MEVCUT = U(["BTC", "ETH", "SOL", "BNB", "DOT", "FIL", "DOGE", "INJ", "ARB", "OP"])
# Tarama sırası: Sharpe'a göre azalan (varlik_tarama.py çıktısı)
SHARPE_SIRA = U(["BNB", "ETH", "ARB", "SOL", "ADA", "DOGE", "HBAR", "INJ",
                 "AVAX", "FIL", "NEAR", "OP", "APT", "DOT", "BTC"])

KOMPOZISYONLAR = [
    ("Mevcut 10",                    MEVCUT),
    ("Mevcut 10 − BTC",              [s for s in MEVCUT if s != "BTCUSDT"]),
    ("Mevcut 10 − BTC − DOT",        [s for s in MEVCUT if s not in ("BTCUSDT", "DOTUSDT")]),
    ("En iyi 8 (Sharpe)",            SHARPE_SIRA[:8]),
    ("En iyi 12 (Sharpe)",           SHARPE_SIRA[:12]),
    ("Kriteri geçen 14",             SHARPE_SIRA[:14]),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sira", action="store_true", help="sıralama duyarlılığı testi")
    ap.add_argument("--tavan", type=int, default=4, help="maks eşzamanlı pozisyon")
    args = ap.parse_args()

    P = CONFIG.strategy
    print(f"PORTFÖY KOMPOZİSYONU — tek bakiye $10.000, maks {args.tavan} pozisyon")
    print(f"Ayar: Donchian={P.donchian_period} {CONFIG.leverage}x risk=%{CONFIG.risk_pct*100:.0f} "
          f"eşik={SCORE_THRESHOLD}\n")

    # tüm gereken sembollerin verisi bir kez hazırlanır
    gerekli = sorted({s for _, lst in KOMPOZISYONLAR for s in lst} | set(SHARPE_SIRA))
    tum_veri = veri_hazirla(gerekli, P, "2022-01-01", "2026-01-01")

    rows = []
    for ad, liste in KOMPOZISYONLAR:
        veri = {s: tum_veri[s] for s in liste}
        for tavan in (None, args.tavan):
            r = run_portfoy(veri, P, ad=f"{ad} · tavan {tavan or '∞'}",
                            leverage=CONFIG.leverage, risk_pct=CONFIG.risk_pct,
                            max_daily_loss_pct=CONFIG.max_daily_loss_pct,
                            max_positions=tavan)
            x = r.satir()
            yil = 4.0
            x["CAGR %"] = round(((r.bitis / r.baslangic) ** (1 / yil) - 1) * 100, 1)
            x["Varlık"] = len(liste)
            rows.append(x)

    df = pd.DataFrame(rows)[["Senaryo", "Varlık", "Getiri %", "CAGR %", "MaxDD %",
                             "Sharpe", "İşlem", "Kazanma %", "PF",
                             "Maks eşzamanlı", "Ort eşzamanlı"]]
    print("=" * 118)
    print("  KOMPOZİSYON KARŞILAŞTIRMASI")
    print("=" * 118)
    print(df.to_string(index=False))

    if args.sira:
        print("\n" + "=" * 118)
        print("  SIRALAMA DUYARLILIĞI — tavan doluyken öncelik listenin başındakilere geçer")
        print("=" * 118)
        liste = SHARPE_SIRA[:12]
        varyant = [
            ("Sharpe sırası (iyi→kötü)", liste),
            ("Ters sıra (kötü→iyi)", list(reversed(liste))),
            ("Alfabetik", sorted(liste)),
        ]
        srows = []
        for ad, lst in varyant:
            veri = {s: tum_veri[s] for s in lst}
            r = run_portfoy(veri, P, ad=ad, leverage=CONFIG.leverage,
                            risk_pct=CONFIG.risk_pct,
                            max_daily_loss_pct=CONFIG.max_daily_loss_pct,
                            max_positions=args.tavan)
            srows.append(r.satir())
        print(pd.DataFrame(srows)[["Senaryo", "Getiri %", "MaxDD %", "Sharpe", "İşlem"]]
              .to_string(index=False))
        print("\n  Fark büyükse: sıralama sonucu belirliyor demektir — bu bir")
        print("  kırılganlıktır, gerçek hayatta hangi sinyalin önce geleceği bilinemez.")


if __name__ == "__main__":
    main()
