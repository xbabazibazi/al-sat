"""MARJİNAL KATKI — "bu varlığı eklersek portföy iyileşir mi?"

İzole tarama (`aday_tarama.py`) "şu varlık iyi" der. Portföyde sorulan soru bu
DEĞİLDİR. 10 paritenin ortalama ikili korelasyonu 0.681; tek başına parlayan bir
varlık, sepete eklendiğinde çeşitlendirme sağlamayıp yalnızca aynı bahsi bir
yerden daha oynamak olabilir. Tek geçerli test: mevcut portföye EKLEYİP tek
bakiyeyle ölçmek.

Bu dosya beş sorguyu birlikte çalıştırır. Bir aday hepsini geçmeden kabul edilmez:

  1) ÜÇ KAPI       — tam dönem + 1. yarı + 2. yarı, ÜÇÜNDE de ΔSharpe > 0.
  2) PLACEBO       — kriteri GEÇEMEYEN varlıklar da eklenir. Adayın kazancı bu
                     grubun en iyisinden belirgin yüksek değilse, ölçtüğümüz şey
                     "varlık eklemek" olabilir, o adayın kendi kenarı değil.
  3) SIRALAMA      — pozisyon tavanı doluyken öncelik listenin BAŞINDAKİLERE
                     geçer. Aday baş/orta/son üç yerde de kazandırmalı, yoksa
                     ölçtüğümüz şey sıralama şansıdır.
  4) ETKİLEŞİM     — en zayıf mevcut parite çıkarıldığında aday HÂLÂ katkı
                     veriyor mu? Vermiyorsa adayın "kenarı" aslında kötü pariteyi
                     slot rekabetinde dışarıda bırakmaktır; bu kırılgan bir
                     gerekçedir, adayın kendi başarısı değildir.
  5) YOĞUNLAŞMA    — kârın tepe birkaç işleme sıkışması trend takibinde
                     NORMALDİR. Anormal olan, adayın portföyün geri kalanından
                     SAPMASIDIR; o yüzden mutlak eşik değil, medyanla kıyas.

------------------------------------------------------------------------------
2026-09-07 TARAMASININ SONUCU (bu araç böyle kullanılır):
516 vadeli pariteden 43'ü ölçüldü, izole kriteri geçen 10 aday portföyde sınandı.
Dokuzu ZARAR verdi (izole taramanın yıldızı WLD dahil: Sharpe 1.01 ama ΔSharpe
yalnızca +0.03). Tek FET üç kapıyı da geçti: ΔSharpe +0.15, placebo grubunun en
iyisi +0.02 (7 kat ayrışma), sıralamadan bağımsız (+0.14/+0.17/+0.15), yoğunlaşma
portföyün EN DÜŞÜĞÜ (%81 vs medyan %178).

Ama 4. sorgu FET'i düşürdü:
    A: mevcut 10          Sharpe 0.93
    B: +FET (11)          Sharpe 1.08   (+0.15)
    C: −BTC (9)           Sharpe 1.03   (+0.10)
    D: −BTC +FET (10)     Sharpe 1.01   ← B ve C'nin İKİSİNDEN de kötü
İki etki gerçek ve bağımsız olsaydı D ≈ +0.25 olmalıydı. BTC çıkınca FET katkısı
NEGATİFE dönüyor (1.03 → 1.01). Yani FET'in görünen değeri kendi kenarından değil,
BTC'nin kötü işlemlerini slot rekabetinde engellemesinden geliyor. REDDEDİLDİ.

Ders: 1-3. sorguları geçen bir aday bile 4. sorguda düşebilir. Sırayı atlama.
------------------------------------------------------------------------------

    python -m backtest.marjinal_katki
    python -m backtest.marjinal_katki --aday FETUSDT,WLDUSDT
    python -m backtest.marjinal_katki --zayif BTCUSDT     # etkileşim testi için
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG, DATA_DIR  # noqa: E402
from backtest.data import download_klines  # noqa: E402
from backtest.portfoy import run_portfoy, veri_hazirla  # noqa: E402
from backtest.kar_cikis import CANLI_PARITELER  # noqa: E402

TARAMA = DATA_DIR / "aday_tarama.csv"
DONEMLER = [("TAM", "2022-01-01", "2026-01-01"),
            ("1.yarı", "2022-01-01", "2024-01-01"),
            ("2.yarı", "2024-01-01", "2026-01-01")]


def veri_var_mi(sembol: str, start: str, end: str) -> bool:
    """Sembolün o dönemde ISINMA dahil yeterli barı var mı?

    Neden gerekli: yeni listelenen varlıkların (WIF 2024, ARB 2023 …) erken
    dönem CSV'si 0 satırdır. Boş veri hem merge_asof'u çökertir, hem de asıl
    tehlikeli olanı: sessizce ΔSharpe=0.00 üretip "katkı vermedi" gibi okunur.
    Oysa doğrusu "SINANAMADI"dır ve o kapı GEÇİLMEMİŞ sayılmalıdır — geçmişi
    olmayan bir varlık o dönemde sağlamlığını kanıtlayamaz.
    """
    try:
        return len(download_klines(sembol, "4h", start, end)) > CONFIG.strategy.warmup_bars
    except Exception:
        return False


def kos(veri, liste, ad="x"):
    """run_portfoy'u canlı ayarlarla çağırır."""
    return run_portfoy({k: veri[k] for k in liste}, CONFIG.strategy, ad=ad,
                       leverage=CONFIG.leverage, risk_pct=CONFIG.risk_pct,
                       max_daily_loss_pct=CONFIG.max_daily_loss_pct,
                       max_positions=CONFIG.max_concurrent_positions)


def adaylari_bul(elle: str | None) -> list[str]:
    if elle:
        return [s.strip().upper() for s in elle.split(",") if s.strip()]
    if not TARAMA.exists():
        sys.exit(f"{TARAMA} yok — önce: python -m backtest.aday_tarama")
    d = pd.read_csv(TARAMA)
    k = (d["Sharpe"] >= 0.30) & (d["Getiri %"] > 0) & (d["PF"] >= 1.10)
    return [s for s in d[k]["sembol"] if s not in CANLI_PARITELER]


def placebo_bul(n: int) -> list[str]:
    """Kriteri GEÇEMEYEN varlıklardan kontrol grubu."""
    if not TARAMA.exists():
        return []
    d = pd.read_csv(TARAMA)
    kotu = d[d["Sharpe"] < 0.30]
    return [s for s in kotu["sembol"] if s not in CANLI_PARITELER][:n]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aday", default=None, help="virgüllü sembol listesi (yoksa taramadan)")
    ap.add_argument("--zayif", default="BTCUSDT", help="etkileşim testinde çıkarılacak parite")
    ap.add_argument("--placebo", type=int, default=8, help="placebo grup boyutu (0=kapat)")
    args = ap.parse_args()

    MEV = list(CANLI_PARITELER)
    adaylar = adaylari_bul(args.aday)
    placebo = placebo_bul(args.placebo) if args.placebo else []
    if not adaylar:
        sys.exit("Sınanacak aday yok.")

    print(f"Mevcut portföy : {len(MEV)} parite, tavan {CONFIG.max_concurrent_positions}, "
          f"{CONFIG.leverage}x, risk %{CONFIG.risk_pct*100:.0f}")
    print(f"Aday           : {', '.join(a.replace('USDT','') for a in adaylar)}")
    print(f"Placebo        : {', '.join(p.replace('USDT','') for p in placebo) or '—'}\n")

    sonuc, korel, sinanamadi = {}, {}, {}

    for ad, s, e in DONEMLER:
        # Geçmişi o döneme yetmeyenleri AYIKLA — boş veri hem çökertir hem de
        # ΔSharpe=0.00 diye sessizce "nötr katkı" gibi okunur (bkz. veri_var_mi).
        var = [x for x in adaylar + placebo if veri_var_mi(x, s, e)]
        yok = [x for x in adaylar + placebo if x not in var]
        for x in yok:
            sinanamadi.setdefault(x, []).append(ad)

        veri = veri_hazirla(MEV + var, CONFIG.strategy, s, e)
        if ad == "TAM":
            ret = pd.DataFrame({k: d.set_index("open_time")["close"].pct_change()
                                for k, d in veri.items()}).dropna()
            for a in adaylar:
                korel[a] = float(np.mean([ret[a].corr(ret[m]) for m in MEV])) \
                    if a in ret.columns else float("nan")
            veri_tam = veri
        sonuc[(ad, "TABAN")] = kos(veri, MEV, "taban").satir()
        for a in var:
            sonuc[(ad, a)] = kos(veri, MEV + [a], a).satir()
        t = sonuc[(ad, "TABAN")]
        print(f"  [{ad}] taban: getiri %{t['Getiri %']} Sharpe {t['Sharpe']} "
              f"MaxDD %{t['MaxDD %']}"
              + (f"   · sınanamayan: {', '.join(y.replace('USDT','') for y in yok)}"
                 if yok else ""))

    # ---------------------------------------------------------------- 1) üç kapı
    rows = []
    for a in adaylar:
        r = {"Aday": a.replace("USDT", ""), "Korelasyon": round(korel[a], 3)}
        gecti = 0
        for ad, _, _ in DONEMLER:
            if (ad, a) not in sonuc:          # o dönemde geçmişi yok
                r[f"ΔSharpe {ad}"] = None     # kapı GEÇİLMEMİŞ sayılır
                continue
            d = round(sonuc[(ad, a)]["Sharpe"] - sonuc[(ad, "TABAN")]["Sharpe"], 3)
            r[f"ΔSharpe {ad}"] = d
            gecti += d > 0
        if ("TAM", a) in sonuc:
            r["ΔGetiri pp"] = round(sonuc[("TAM", a)]["Getiri %"]
                                    - sonuc[("TAM", "TABAN")]["Getiri %"], 1)
            r["ΔMaxDD pp"] = round(sonuc[("TAM", a)]["MaxDD %"]
                                   - sonuc[("TAM", "TABAN")]["MaxDD %"], 1)
        r["Kapı"] = f"{gecti}/3" + (" (eksik geçmiş)" if a in sinanamadi else "")
        rows.append(r)
    df = pd.DataFrame(rows).sort_values("ΔSharpe TAM", ascending=False)
    print("\n" + "=" * 104)
    print("  1) ÜÇ KAPI — mevcut portföye EKLENİNCE   (ΔMaxDD negatif = daha derin düşüş = KÖTÜ)")
    print("=" * 104)
    print(df.to_string(index=False))

    kalan = [a for a in adaylar
             if df.loc[df.Aday == a.replace("USDT", ""), "Kapı"].iloc[0] == "3/3"]
    if sinanamadi:
        print("\n  Eksik geçmiş yüzünden bazı dönemlerde SINANAMAYAN adaylar "
              "(o kapılar geçilmemiş sayıldı):")
        for x, dn in sinanamadi.items():
            if x in adaylar:
                print(f"    {x.replace('USDT',''):8s} → {', '.join(dn)}")
    print(f"\n  Üç kapıyı geçen: {', '.join(k.replace('USDT','') for k in kalan) or 'HİÇBİRİ'}")
    if not kalan:
        print("\n  Sonuç: eklenecek varlık yok. Mevcut liste korunur.")
        return

    # ---------------------------------------------------------------- 2) placebo
    if placebo:
        p = [{"Placebo": x.replace("USDT", ""),
              "ΔSharpe": round(sonuc[("TAM", x)]["Sharpe"]
                               - sonuc[("TAM", "TABAN")]["Sharpe"], 3)}
             for x in placebo if ("TAM", x) in sonuc]
        pdf = pd.DataFrame(p).sort_values("ΔSharpe", ascending=False)
        print("\n" + "=" * 104)
        print("  2) PLACEBO — kriteri geçemeyen varlıklar da katkı veriyor mu?")
        print("=" * 104)
        print(pdf.to_string(index=False))
        en_iyi = pdf["ΔSharpe"].max()
        print(f"\n  Placebo en iyisi: {en_iyi:+.3f}")
        for k in kalan:
            kd = df.loc[df.Aday == k.replace("USDT", ""), "ΔSharpe TAM"].iloc[0]
            kat = kd / en_iyi if en_iyi > 0 else float("inf")
            print(f"  {k.replace('USDT',''):6s}: {kd:+.3f}  "
                  f"({'✔ ' if kd > 2 * max(en_iyi, 0.01) else '⚠ zayıf ayrışma — '}"
                  f"placebo'nun {kat:.1f} katı)")

    # ------------------------------------------------------------- 3) sıralama
    print("\n" + "=" * 104)
    print("  3) SIRALAMA DUYARLILIĞI — tavan doluyken öncelik listenin başındakilere geçer")
    print("=" * 104)
    taban_s = sonuc[("TAM", "TABAN")]["Sharpe"]
    for k in kalan:
        srow = []
        for yer, lst in (("başta", [k] + MEV),
                         ("ortada", MEV[:len(MEV) // 2] + [k] + MEV[len(MEV) // 2:]),
                         ("sonda", MEV + [k])):
            x = kos(veri_tam, lst, yer).satir()
            srow.append({"Aday": k.replace("USDT", ""), "Yer": yer,
                         "Sharpe": x["Sharpe"], "ΔSharpe": round(x["Sharpe"] - taban_s, 3)})
        sdf = pd.DataFrame(srow)
        print(sdf.to_string(index=False))
        yay = sdf["ΔSharpe"].max() - sdf["ΔSharpe"].min()
        print(f"    yayılım {yay:.3f} — "
              + ("✔ sıralamadan bağımsız\n" if yay < 0.10 else
                 "⚠ sıralama sonucu belirliyor, KIRILGAN\n"))

    # ------------------------------------------------------------ 4) etkileşim
    print("=" * 104)
    print(f"  4) ETKİLEŞİM — en zayıf parite ({args.zayif.replace('USDT','')}) çıkarılınca aday hâlâ katkı veriyor mu?")
    print("=" * 104)
    kalansiz = [s for s in MEV if s != args.zayif]
    a_row = kos(veri_tam, MEV, "A").satir()
    c_row = kos(veri_tam, kalansiz, "C").satir()
    erows = [{"Senaryo": "A: mevcut", "Sharpe": a_row["Sharpe"], "ΔvsA": 0.0},
             {"Senaryo": f"C: −{args.zayif.replace('USDT','')}", "Sharpe": c_row["Sharpe"],
              "ΔvsA": round(c_row["Sharpe"] - a_row["Sharpe"], 3)}]
    for k in kalan:
        b = kos(veri_tam, MEV + [k], "B").satir()
        d = kos(veri_tam, kalansiz + [k], "D").satir()
        erows += [
            {"Senaryo": f"B: +{k.replace('USDT','')}", "Sharpe": b["Sharpe"],
             "ΔvsA": round(b["Sharpe"] - a_row["Sharpe"], 3)},
            {"Senaryo": f"D: −{args.zayif.replace('USDT','')} +{k.replace('USDT','')}",
             "Sharpe": d["Sharpe"], "ΔvsA": round(d["Sharpe"] - a_row["Sharpe"], 3)},
        ]
        print(pd.DataFrame(erows).to_string(index=False))
        bagimsiz = d["Sharpe"] > c_row["Sharpe"]
        print(f"\n    D ({d['Sharpe']}) > C ({c_row['Sharpe']}) ? "
              + ("✔ EVET — katkı bağımsız, aday KABUL edilebilir"
                 if bagimsiz else
                 "✘ HAYIR — adayın değeri zayıf pariteyi dışarıda bırakmaktan geliyor, REDDET"))
        erows = erows[:2]

    print("\n  NOT: Çoklu test uyarısı — bu araç onlarca aday × üç dönem deniyor.")
    print("  Bir aramanın KAZANANI daima olduğundan iyi görünür; gerçekleşen katkı")
    print("  ölçülenden küçük olacaktır. Kabul kararı canlı veriyle doğrulanmalı.")


if __name__ == "__main__":
    main()
