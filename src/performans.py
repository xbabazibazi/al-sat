"""PERFORMANS OKUMASI — "kaybediyor muyuz, yoksa normal mi?" sorusunun cevabı.

Neden ayrı bir modül: bu stratejinin doğası gereği işlemlerin ÇOĞU zarar eder.
Kâr, az sayıda büyük kazançtan gelir. Bu yüzden "üst üste 3 zarar" hem gerçek
hem de tamamen beklenen bir olaydır — ama panelde yalnızca son işlemler
görünürse insan (haklı olarak) "bot bozuldu" diye okur ve ÇALIŞAN sistemi
değiştirmek ister. Pozitif beklentili bir sistemi öldürmenin bilinen yolu budur.

Buradaki sayıların tek işi o kararı duyguya değil ölçüye bağlamak:
  beklenti = p×ort_kazanç − (1−p)×ort_zarar
Beklenti artı ise seri zarar bir arıza değil, maliyet kalemidir.

Saf fonksiyon — veritabanı, ağ, zaman yok; test edilmesi kolay olsun diye.
"""
from __future__ import annotations

import math

# Faz 1 kapısı: bu eşiklerin altında/üstünde olmak tek başına karar değildir,
# ama sapma büyükse bakılması gerektiğini söyler. (backtest/portfoy.py ölçümü)
HEDEF_KAZANMA = (32.0, 46.0)   # %
HEDEF_ODEME = 1.8              # ort. kazanç ÷ ort. zarar


def _seri(pnl_sirali: list[float]) -> int:
    """Sondan geriye aynı işaretli kaç işlem var: +3 = 3 kazanç, −3 = 3 zarar."""
    if not pnl_sirali:
        return 0
    son_kazanc = pnl_sirali[-1] > 0
    n = 0
    for x in reversed(pnl_sirali):
        if (x > 0) != son_kazanc:
            break
        n += 1
    return n if son_kazanc else -n


def _beklenen_en_uzun_seri(n: int, q: float) -> float:
    """n işlemde beklenen EN UZUN üst üste zarar serisi ≈ ln(n)/ln(1/q).

    "3 zarar üst üste alarm mı?" sorusunun dürüst cevabı burada: kazanma
    oranı düşük bir trend sisteminde uzun zarar serileri kural dışı değil,
    kuralın kendisidir.
    """
    if n <= 0 or not (0.0 < q < 1.0):
        return 0.0
    return math.log(n) / math.log(1.0 / q)


def ozet(pnl_sirali: list[float]) -> dict:
    """Kapanmış işlemlerin K/Z listesi (ESKİDEN YENİYE) → performans tablosu."""
    n = len(pnl_sirali)
    kazanclar = [x for x in pnl_sirali if x > 0]
    zararlar = [x for x in pnl_sirali if x <= 0]
    kazanma = len(kazanclar) / n * 100 if n else 0.0
    ort_kazanc = sum(kazanclar) / len(kazanclar) if kazanclar else 0.0
    ort_zarar = abs(sum(zararlar) / len(zararlar)) if zararlar else 0.0
    odeme = ort_kazanc / ort_zarar if ort_zarar else 0.0
    p = kazanma / 100.0
    beklenti = p * ort_kazanc - (1 - p) * ort_zarar

    # Gerçekleşen K/Z'nin tepe noktasından bugünkü geri çekilme. Açık
    # pozisyonlar burada YOK — bu bankaya girmiş parayla ilgili bir ölçüdür.
    tepe = kum = 0.0
    dusus = 0.0
    for x in pnl_sirali:
        kum += x
        tepe = max(tepe, kum)
        dusus = min(dusus, kum - tepe)

    seri = _seri(pnl_sirali)
    q = 1 - p
    # Bundan sonraki |seri| işlemin hepsinin zarar etme olasılığı: q^k.
    # Yani "bu seri ne kadar sıra dışı" sorusunun tek satırlık cevabı.
    seri_olasilik = (q ** abs(seri) * 100.0) if (seri < 0 and 0 < q < 1) else 0.0

    return {
        "n": n,
        "kazanan": len(kazanclar),
        "kaybeden": len(zararlar),
        "kazanma_orani": round(kazanma, 1),
        "ort_kazanc": round(ort_kazanc, 2),
        "ort_zarar": round(ort_zarar, 2),
        "odeme_orani": round(odeme, 2),          # ort. kazanç ÷ ort. zarar
        "beklenti": round(beklenti, 2),          # işlem başına beklenen K/Z
        "toplam": round(sum(pnl_sirali), 2),
        "tepeden_dusus": round(dusus, 2),        # gerçekleşen K/Z geri çekilmesi
        "seri": seri,                            # + kazanç serisi, − zarar serisi
        "seri_olasilik": round(seri_olasilik, 1),
        "beklenen_en_uzun_zarar": round(_beklenen_en_uzun_seri(n, q), 1),
        "hedef_kazanma": list(HEDEF_KAZANMA),
        "hedef_odeme": HEDEF_ODEME,
        "yorum": yorum(n, beklenti, odeme, seri),
    }


def yorum(n: int, beklenti: float, odeme: float, seri: int) -> str:
    """Tek cümlelik okuma. Abartmaz, yatıştırmaz — ne görüyorsak onu söyler."""
    if n == 0:
        return "Henüz kapanmış işlem yok."
    if n < 30:
        temel = (f"{n} işlem istatistiksel olarak ANLAMSIZ — "
                 f"bu sayılarla strateji değiştirmek en büyük risk.")
    elif beklenti > 0:
        temel = f"Beklenti pozitif ({beklenti:+.2f}/işlem) — sistem çalışıyor."
    else:
        temel = (f"Beklenti NEGATİF ({beklenti:+.2f}/işlem) — "
                 f"bu kadar işlemden sonra incelenmeli.")
    if seri <= -3:
        temel += f" {-seri} üst üste zarar var; bu sistemde olağan."
    if odeme and odeme < HEDEF_ODEME and n >= 30:
        temel += f" Ödeme oranı {odeme:.2f} < {HEDEF_ODEME} hedefi."
    return temel
