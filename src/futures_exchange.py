"""Binance USDT-M VADELİ emir geçidi — gerçek para yolunun kapısı.

Bu dosya `exchange.py`'nin (spot) vadeli karşılığıdır. Ayrı durmasının sebebi
teknik: vadeli uçlar spot'tan TAMAMEN farklıdır (`futures_*`), filtreler ayrı
bir sözlükten gelir, kaldıraç ve pozisyon modu diye kavramlar vardır. Spot
kodunu "biraz düzenleyip" kullanmak sessiz hatalar üretirdi.

DEĞİŞMEZ KURAL — STOPSUZ POZİSYON OLMAZ:
  Kâğıt modda "borsa" bot sürecinin kendisiydi; stop her turda kod içinde
  kontrol ediliyordu. CANLIDA BU KABUL EDİLEMEZ: bot çökerse, sunucu
  kapanırsa, internet giderse pozisyon korumasız kalır.
  Bu yüzden stop, Binance'in kendi sunucusunda duran gerçek bir
  STOP_MARKET emridir (closePosition=true). Biz ölsek bile o emir çalışır.

  Bunun bir adım ötesi de var: pozisyon açıldıktan SONRA stop kurulamazsa
  (ağ hatası, filtre reddi) pozisyon ANINDA kapatılır. "Stopsuz ama açık"
  bir an bile kabul edilmez — bkz. giris_ve_stop().

NE YAPMAZ: strateji kararı vermez. Ne zaman girileceğine, nereye stop
konacağına strateji katmanı karar verir; burası yalnızca emri iletir ve
sonucu DÜRÜSTÇE raporlar (başarısızlığı başarı gibi göstermez).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from .config import Config

log = logging.getLogger("futures_exchange")


@dataclass(frozen=True)
class VadeliFiltre:
    step_size: float        # LOT_SIZE stepSize — miktar adımı
    min_qty: float          # LOT_SIZE minQty
    tick_size: float        # PRICE_FILTER tickSize — fiyat adımı
    min_notional: float     # MIN_NOTIONAL — vadelide genelde 5 USDT


@dataclass(frozen=True)
class Dolum:
    ort_fiyat: float
    miktar: float
    emir_id: int


def asagi_yuvarla(deger: float, adim: float) -> float:
    """Borsa adımına AŞAĞI yuvarlar. Yukarı yuvarlamak 'yetersiz bakiye' veya
    LOT_SIZE reddi üretir."""
    if adim <= 0:
        return deger
    return math.floor(deger / adim + 1e-12) * adim


def fiyat_yuvarla(deger: float, tick: float) -> float:
    """Fiyatı tick adımına yuvarlar. Stop fiyatı tick'e uymazsa emir REDDEDİLİR."""
    if tick <= 0:
        return deger
    return round(round(deger / tick) * tick, 10)


class FuturesBroker:
    """Gerçek Binance USDT-M hesabına emir gönderir (testnet veya canlı)."""

    def __init__(self, cfg: Config):
        from binance.client import Client  # geç import: kâğıt modda gerekmez

        self.cfg = cfg
        self.canli = cfg.mode == "futures_live"
        if self.canli:
            self.client = Client(cfg.live_key, cfg.live_secret)
            log.warning("VADELİ CANLI MOD — emirler GERÇEK PARA ile gönderilecek")
        else:
            self.client = Client(cfg.testnet_key, cfg.testnet_secret, testnet=True)
            log.info("Vadeli TESTNET modu — gerçek para riski yok")
        self._filtre_onbellek: dict[str, VadeliFiltre] = {}
        self._kaldirac_ayarlandi: set[str] = set()

    # ------------------------------------------------------------------ filtreler
    def filtreler(self, symbol: str) -> VadeliFiltre:
        """Emir BU borsaya gittiği için filtreler de BURADAN alınır (spot'unki farklı)."""
        if symbol in self._filtre_onbellek:
            return self._filtre_onbellek[symbol]
        bilgi = self.client.futures_exchange_info()
        for s in bilgi.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            f = {x["filterType"]: x for x in s.get("filters", [])}
            lot = f.get("LOT_SIZE", {})
            self._filtre_onbellek[symbol] = VadeliFiltre(
                step_size=float(lot.get("stepSize", 0.001)),
                min_qty=float(lot.get("minQty", 0.001)),
                tick_size=float(f.get("PRICE_FILTER", {}).get("tickSize", 0.01)),
                min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
            )
            return self._filtre_onbellek[symbol]
        raise ValueError(f"{symbol} vadeli piyasada bulunamadı")

    # ------------------------------------------------------------------ hesap
    def bakiye_usdt(self) -> float:
        for b in self.client.futures_account_balance():
            if b.get("asset") == "USDT":
                return float(b.get("availableBalance", b.get("balance", 0)))
        return 0.0

    def pozisyon(self, symbol: str) -> Optional[dict]:
        """Borsadaki GERÇEK pozisyon. Yoksa None. Mutabakatın temeli budur:
        kendi kaydımıza değil, borsanın söylediğine bakarız."""
        for p in self.client.futures_position_information(symbol=symbol):
            miktar = float(p.get("positionAmt", 0))
            if abs(miktar) > 0:
                return {
                    "symbol": symbol,
                    "miktar": abs(miktar),
                    "yon": "LONG" if miktar > 0 else "SHORT",
                    "giris": float(p.get("entryPrice", 0)),
                    "kaldirac": float(p.get("leverage", 1)),
                    "likidasyon": float(p.get("liquidationPrice", 0) or 0),
                }
        return None

    def tek_yon_modu_mu(self) -> bool:
        """Hedge modda aynı sembolde hem long hem short açılabilir ve bizim
        'tek pozisyon' varsayımımız çöker. Canlı öncesi mutlaka doğrulanmalı."""
        try:
            return not bool(self.client.futures_get_position_mode().get("dualSidePosition"))
        except Exception as e:  # noqa: BLE001
            log.error("Pozisyon modu okunamadı: %s", e)
            return False

    def kaldirac_ayarla(self, symbol: str, kaldirac: int) -> bool:
        """Her girişten ÖNCE çağrılır. Borsadaki kaldıraç bizim varsaydığımızdan
        farklıysa pozisyon boyutu ve likidasyon mesafesi yanlış hesaplanır."""
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=int(kaldirac))
            self._kaldirac_ayarlandi.add(symbol)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("[%s] Kaldıraç %sx ayarlanamadı: %s", symbol, kaldirac, e)
            return False

    # ------------------------------------------------------------------ emirler
    def _piyasa_emri(self, symbol: str, taraf: str, miktar: float) -> Optional[Dolum]:
        try:
            emir = self.client.futures_create_order(
                symbol=symbol, side=taraf, type="MARKET", quantity=miktar)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] %s MARKET emri reddedildi: %s", symbol, taraf, e)
            return None
        dolan = float(emir.get("executedQty", 0) or 0)
        if dolan <= 0:
            log.error("[%s] %s emri dolmadı: %s", symbol, taraf, emir)
            return None
        # avgPrice bazı yanıtlarda '0' gelir; o zaman cumQuote'tan türetilir.
        ort = float(emir.get("avgPrice", 0) or 0)
        if ort <= 0:
            kote = float(emir.get("cumQuote", 0) or 0)
            ort = kote / dolan if dolan else 0.0
        return Dolum(ort_fiyat=ort, miktar=dolan, emir_id=int(emir.get("orderId", 0)))

    def stop_kur(self, symbol: str, yon: str, stop_fiyat: float) -> Optional[int]:
        """Borsa tarafında STOP_MARKET kurar (closePosition=true).

        closePosition neden: miktar belirtmeye gerek kalmaz, pozisyonun TAMAMINI
        kapatır ve kısmi dolumlarda miktar uyuşmazlığı riski doğmaz. Ayrıca
        reduceOnly olduğu için ters yönde yeni pozisyon açma ihtimali yoktur.
        """
        tick = self.filtreler(symbol).tick_size
        fiyat = fiyat_yuvarla(stop_fiyat, tick)
        kapatma_yonu = "SELL" if yon == "LONG" else "BUY"
        try:
            emir = self.client.futures_create_order(
                symbol=symbol, side=kapatma_yonu, type="STOP_MARKET",
                stopPrice=fiyat, closePosition=True, workingType="MARK_PRICE")
            return int(emir.get("orderId", 0))
        except Exception as e:  # noqa: BLE001
            log.error("[%s] STOP_MARKET kurulamadı (%s @ %s): %s",
                      symbol, kapatma_yonu, fiyat, e)
            return None

    def stop_var_mi(self, symbol: str) -> Optional[dict]:
        """Borsada duran stop emrini döndürür. HER TURDA kontrol edilmeli:
        emir elle iptal edilmiş, tetiklenmiş veya hiç kurulmamış olabilir."""
        try:
            for e in self.client.futures_get_open_orders(symbol=symbol):
                if e.get("type") in ("STOP_MARKET", "STOP"):
                    return {"id": int(e.get("orderId", 0)),
                            "stop": float(e.get("stopPrice", 0))}
        except Exception as e:  # noqa: BLE001
            log.error("[%s] Açık emirler okunamadı: %s", symbol, e)
        return None

    def emir_iptal(self, symbol: str, emir_id: int) -> bool:
        try:
            self.client.futures_cancel_order(symbol=symbol, orderId=emir_id)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("[%s] Emir %s iptal edilemedi: %s", symbol, emir_id, e)
            return False

    # -------------------------------------------------- birleşik güvenli işlemler
    def giris_ve_stop(self, symbol: str, yon: str, miktar: float,
                      stop_fiyat: float) -> Optional[dict]:
        """Pozisyon açar ve stopunu kurar — İKİSİ BİR ARADA, ayrılmaz.

        Stop kurulamazsa pozisyon DERHAL kapatılır. Gerekçe kullanıcının
        değişmez şartı: "stop loss daima olacak". Stopsuz açık pozisyon,
        kâr ihtimali ne olursa olsun, kabul edilebilir bir durum değildir.
        Kapatma da başarısız olursa bu bir ACİL DURUMDUR: çağıran katman
        kullanıcıyı uyarmak zorundadır (dönen sözlükte 'acil' bayrağı).
        """
        taraf = "BUY" if yon == "LONG" else "SELL"
        dolum = self._piyasa_emri(symbol, taraf, miktar)
        if dolum is None:
            return None

        stop_id = self.stop_kur(symbol, yon, stop_fiyat)
        if stop_id is not None:
            return {"giris": dolum.ort_fiyat, "miktar": dolum.miktar,
                    "stop_id": stop_id, "acil": False}

        log.error("[%s] STOP KURULAMADI — pozisyon derhal kapatılıyor "
                  "(stopsuz pozisyon taşınmaz)", symbol)
        ters = "SELL" if yon == "LONG" else "BUY"
        geri = self._piyasa_emri(symbol, ters, dolum.miktar)
        if geri is None:
            log.critical("[%s] ACİL: pozisyon açık, stop YOK ve kapatılamadı!", symbol)
            return {"giris": dolum.ort_fiyat, "miktar": dolum.miktar,
                    "stop_id": None, "acil": True}
        return None   # açıldı ve güvenle geri kapatıldı: pozisyon yok

    def stop_tasi(self, symbol: str, yon: str, eski_id: Optional[int],
                  yeni_stop: float) -> Optional[int]:
        """İz süren stop: önce YENİSİNİ kur, sonra eskisini iptal et.

        SIRA ÖNEMLİ. Tersi yapılsaydı (önce iptal, sonra kur) aradaki
        milisaniyelerde pozisyon STOPSUZ kalırdı ve tam o anda gelen bir
        fitil korumasız yakalardı. Bu sırada en kötü ihtimal iki stop emrinin
        kısa süre birlikte durmasıdır; ikisi de closePosition olduğu için
        zararsızdır (biri tetiklenince diğeri boşa düşer).
        """
        yeni_id = self.stop_kur(symbol, yon, yeni_stop)
        if yeni_id is None:
            log.error("[%s] Yeni stop kurulamadı — ESKİSİ KORUNUYOR", symbol)
            return eski_id
        if eski_id:
            self.emir_iptal(symbol, eski_id)
        return yeni_id

    def pozisyonu_kapat(self, symbol: str, yon: str, miktar: float,
                        stop_id: Optional[int] = None) -> Optional[Dolum]:
        """Pozisyonu piyasa emriyle kapatır ve bekleyen stopu temizler."""
        ters = "SELL" if yon == "LONG" else "BUY"
        dolum = self._piyasa_emri(symbol, ters, miktar)
        if stop_id:
            self.emir_iptal(symbol, stop_id)
        return dolum
