"""CANLI ÖNCESİ KONTROL — kontrol listesinin çalıştırılabilir hâli.

    python -m src.canli_kontrol                # salt okunur (varsayılan)
    python -m src.canli_kontrol --emir-dene    # testnet'te GERÇEK emir dener

Neden yazılı liste değil de betik: bu projede "herhalde doğrudur" diye
varsayılan her şey yanlış çıktı (cron çalışıyordur, kilit boştur, token
geçerlidir...). Gerçek parayla aynı hatayı yapmayalım — her madde
SORULUR ve cevabı gösterilir.

ÇIKIŞ KODU: 0 = tüm kritik kontroller geçti · 1 = en az bir KRİTİK sorun.
Uyarılar (⚠) çıkışı etkilemez ama okunmalıdır.

GÜVENLİK: --emir-dene DIŞINDA hiçbir emir gönderilmez. --emir-dene ise
canlı modda ÇALIŞMAZ; yalnızca testnet'te izin verilir.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from .config import CONFIG

GECTI, UYARI, KALDI = [], [], []


def gecti(baslik: str, detay: str = "") -> None:
    GECTI.append(baslik)
    print(f"  ✔ {baslik}" + (f"  — {detay}" if detay else ""))


def uyari(baslik: str, detay: str = "") -> None:
    UYARI.append(baslik)
    print(f"  ⚠ {baslik}" + (f"  — {detay}" if detay else ""))


def kaldi(baslik: str, detay: str = "") -> None:
    KALDI.append(f"{baslik} — {detay}" if detay else baslik)
    print(f"  ✘ {baslik}" + (f"  — {detay}" if detay else ""))


def baslik(s: str) -> None:
    print(f"\n{s}")


# --------------------------------------------------------------------- kontroller
def anahtarlar_ve_mod(canli: bool) -> bool:
    baslik("1) MOD VE ANAHTARLAR")
    print(f"  BOT_MODE = {CONFIG.mode}")
    anahtar = CONFIG.live_key if canli else CONFIG.testnet_key
    gizli = CONFIG.live_secret if canli else CONFIG.testnet_secret
    ad = "BINANCE_LIVE_KEY/SECRET" if canli else "BINANCE_TESTNET_KEY/SECRET"
    if not (anahtar and gizli):
        kaldi(f"{ad} eksik", ".env'e yazılmalı")
        return False
    # Anahtarın KENDİSİ asla yazdırılmaz; yalnızca varlığı ve uzunluğu.
    gecti(f"{ad} mevcut", f"anahtar {len(anahtar)} karakter")
    if canli and CONFIG.canli_onay != "EVET_GERCEK_PARA":
        uyari("CANLI_ONAY henüz verilmemiş",
              "gerçek parayla çalışmak için .env'e CANLI_ONAY=EVET_GERCEK_PARA")
    return True


def baglanti_ve_saat(client) -> None:
    baslik("2) BAĞLANTI VE SAAT SAPMASI")
    try:
        t0 = time.time()
        sunucu_ms = int(client.futures_time()["serverTime"])
        gidis_donus = (time.time() - t0) * 1000
    except Exception as e:  # noqa: BLE001
        kaldi("Vadeli API'ye bağlanılamadı", str(e)[:120])
        return
    yerel_ms = time.time() * 1000
    sapma = abs(yerel_ms - sunucu_ms)
    gecti("Vadeli API'ye bağlanıldı", f"gidiş-dönüş {gidis_donus:.0f} ms")
    # Binance imzalı isteklerde 1000 ms'lik pencere kullanır; sapma büyürse
    # emirler "Timestamp for this request..." hatasıyla REDDEDİLİR.
    if sapma > 1000:
        kaldi("Saat sapması çok büyük", f"{sapma:.0f} ms — sunucuda NTP kurulmalı")
    elif sapma > 300:
        uyari("Saat sapması sınırda", f"{sapma:.0f} ms")
    else:
        gecti("Saat sapması güvenli", f"{sapma:.0f} ms")


def api_izinleri(client, canli: bool) -> None:
    baslik("3) API ANAHTAR İZİNLERİ (en kritik başlık)")
    try:
        izin = client.get_account_api_permissions()
    except Exception as e:  # noqa: BLE001
        uyari("İzinler okunamadı", f"{str(e)[:100]} (testnet'te normal olabilir)")
        return

    # PARA ÇEKME İZNİ: bu açıksa anahtarı ele geçiren paranı ÇEKEBİLİR.
    # Botun para çekmeye ihtiyacı yoktur; bu izin asla açılmamalıdır.
    if izin.get("enableWithdrawals"):
        kaldi("PARA ÇEKME İZNİ AÇIK",
              "Binance'te bu izni KAPAT — bot buna ihtiyaç duymaz, "
              "açık kalırsa anahtar sızdığında paran çekilebilir")
    else:
        gecti("Para çekme izni kapalı")

    if izin.get("enableFutures"):
        gecti("Vadeli işlem izni açık")
    else:
        kaldi("Vadeli işlem izni KAPALI", "anahtarda Futures izni açılmalı")

    # IP KISITI: anahtar sızsa bile yalnızca bizim sunucudan kullanılabilsin.
    if izin.get("ipRestrict"):
        gecti("IP kısıtı açık", "anahtar yalnızca izinli IP'lerden kullanılabilir")
    elif canli:
        kaldi("IP kısıtı YOK",
              "canlıda anahtar dünyanın her yerinden kullanılabilir — "
              "sunucunun IP'si beyaz listeye alınmalı")
    else:
        uyari("IP kısıtı yok", "testnet için kabul edilebilir, canlıda şart")


def hesap_durumu(broker) -> float:
    baslik("4) HESAP DURUMU")
    try:
        bakiye = broker.bakiye_usdt()
    except Exception as e:  # noqa: BLE001
        kaldi("Bakiye okunamadı", str(e)[:120])
        return 0.0
    gecti("Vadeli USDT bakiyesi okundu", f"{bakiye:,.2f} USDT")
    if bakiye <= 0:
        uyari("Bakiye sıfır", "testnet'te bedava bakiye alınabilir; canlıda transfer gerekir")

    # Tek yön modu: hedge modda aynı sembolde iki pozisyon olabilir ve botun
    # "sembol başına tek pozisyon" varsayımı çöker.
    if broker.tek_yon_modu_mu():
        gecti("Pozisyon modu: tek yön (one-way)")
    else:
        kaldi("Pozisyon modu HEDGE",
              "bot tek yön varsayar — Binance'te 'Tek Yönlü Mod'a alınmalı")
    return bakiye


def semboller(broker) -> None:
    baslik("5) SEMBOLLER VE FİLTRELER")
    for sym in CONFIG.symbols:
        try:
            f = broker.filtreler(sym)
        except Exception as e:  # noqa: BLE001
            kaldi(f"{sym} filtreleri alınamadı", str(e)[:80])
            continue
        gecti(f"{sym}", f"adım {f.step_size:g} · min {f.min_qty:g} · "
                        f"tick {f.tick_size:g} · min tutar {f.min_notional:g} USDT")


def kaldirac_ve_stop(broker, emir_dene: bool) -> None:
    baslik("6) KALDIRAÇ VE STOP EMRİ")
    sym = CONFIG.symbols[0] if CONFIG.symbols else "BTCUSDT"
    if not emir_dene:
        uyari("Emir denemesi atlandı",
              "gerçek sınama için: python -m src.canli_kontrol --emir-dene (yalnız testnet)")
        return
    if broker.kaldirac_ayarla(sym, int(CONFIG.leverage)):
        gecti(f"{sym} kaldıracı {CONFIG.leverage:.0f}x olarak ayarlandı")
    else:
        kaldi(f"{sym} kaldıracı ayarlanamadı", "izinler veya sembol hatalı olabilir")

    # STOP_MARKET'in gerçekten kabul edildiğini görmek kritik: canlıda ilk
    # kez denemek, "stop kurulamıyor" sürprizini en kötü anda yaşamak demektir.
    # Pozisyon yokken closePosition emri kabul edilir ve hemen iptal edilir.
    poz = broker.pozisyon(sym)
    if poz:
        uyari("Açık pozisyon var, stop denemesi yapılmadı", f"{poz['yon']} {poz['miktar']}")
        return
    try:
        fiyat = float(broker.client.futures_symbol_ticker(symbol=sym)["price"])
    except Exception as e:  # noqa: BLE001
        kaldi("Fiyat alınamadı", str(e)[:100])
        return
    stop_id = broker.stop_kur(sym, "LONG", fiyat * 0.5)   # uzak, tetiklenmez
    if stop_id:
        gecti("STOP_MARKET emri borsada kabul edildi", f"emir {stop_id}")
        broker.emir_iptal(sym, stop_id)
        gecti("Deneme emri iptal edildi")
    else:
        kaldi("STOP_MARKET KURULAMADI",
              "canlıya geçilmemeli — stopsuz pozisyon riski")


def ozet() -> int:
    print("\n" + "=" * 70)
    print(f"  {len(GECTI)} geçti · {len(UYARI)} uyarı · {len(KALDI)} KRİTİK sorun")
    print("=" * 70)
    if KALDI:
        print("\nCANLIYA GEÇİLMEMELİ — çözülmesi gerekenler:")
        for k in KALDI:
            print(f"  ✘ {k}")
        return 1
    if UYARI:
        print("\nUyarılar (okunmalı, engelleyici değil):")
        for u in UYARI:
            print(f"  ⚠ {u}")
    print("\nTeknik kontroller tamam. AMA teknik hazırlık, STRATEJİ hazırlığı")
    print("demek değildir: Faz 1 kapısı (~100 kapanmış işlem) ayrıca geçilmeli.")
    return 0


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    emir_dene = "--emir-dene" in sys.argv
    canli = CONFIG.mode == "futures_live"

    print("=" * 70)
    print("  CANLI ÖNCESİ KONTROL — Binance USDT-M vadeli")
    print(f"  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 70)

    if emir_dene and canli:
        print("\n✘ --emir-dene CANLI modda kullanılamaz (gerçek para).")
        print("  Emir denemesi yalnızca testnet içindir.")
        return 1

    if not anahtarlar_ve_mod(canli):
        return ozet()

    try:
        from .futures_exchange import FuturesBroker
        broker = FuturesBroker(CONFIG)
    except Exception as e:  # noqa: BLE001
        kaldi("Borsa istemcisi kurulamadı", str(e)[:150])
        return ozet()

    baglanti_ve_saat(broker.client)
    api_izinleri(broker.client, canli)
    hesap_durumu(broker)
    semboller(broker)
    kaldirac_ve_stop(broker, emir_dene)
    return ozet()


if __name__ == "__main__":
    raise SystemExit(main())
