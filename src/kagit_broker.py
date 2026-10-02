"""KAĞIT BROKER — FuturesBroker'ın sanal ikizi, BİREBİR aynı arayüz.

NEDEN BÖYLE: kullanıcının şartı "altyapı doğrudan borsanın kendisine
geçebilecek şekilde kurgulansın". Bunu sağlamanın tek dürüst yolu, kâğıt
ile gerçek arasındaki farkı TEK bir yere hapsetmek:

    ScalpTrader ──► broker arayüzü ──┬──► KagitBroker    (bu dosya)
                                     └──► FuturesBroker  (gerçek Binance)

Trader hangisiyle çalıştığını BİLMEZ. Geçiş `scalp_mode` ayarıdır, kod
değişikliği değil. Alternatif (kâğıt için ayrı mantık yazmak) kaçınılmaz
olarak sapar: canlıda ilk kez ortaya çıkan davranış farkı, en pahalı hata
türüdür.

SANAL OLANIN SINIRLARI — açıkça yazılıyor ki kâğıt sonuçları fazla iyimser
okunmasın:
  • Emir defteri yok: kayma sabit varsayılır (SLIPPAGE).
  • Dolum hep tamdır; gerçekte kısmi dolum olabilir.
  • Komisyon TAKER varsayılır. Maker (limit) girişle gerçek maliyet daha
    düşüktür — yani kâğıt sonucu KÖTÜMSER tarafta kalır. Bu bilinçli:
    kâğıtta iyimser olmak canlıda sürpriz üretir.
  • Likidasyon modellenmez; stop mesafesi likidasyondan çok uzak olduğu
    için (bkz. scalp_trader maliyet kapısı) pratikte bağlayıcı değil.
"""
from __future__ import annotations

import logging
from typing import Optional

from .futures_exchange import Dolum, VadeliFiltre, asagi_yuvarla, fiyat_yuvarla
from .state import StateStore

log = logging.getLogger("kagit")

SLIPPAGE = 0.0005          # her yönde %0.05 — vadelide muhafazakâr varsayım
TAKER_FEE = 0.0005         # USDT-M taker %0.05

# Gerçek borsa filtreleri yerine makul varsayılanlar. Kâğıtta amaç emir
# reddi simüle etmek değil, boyutlamanın aynı yuvarlama kurallarından
# geçmesini sağlamak — canlıya geçince miktar hesabı değişmesin.
VARSAYILAN_FILTRE = VadeliFiltre(step_size=0.001, min_qty=0.001,
                                 tick_size=0.0001, min_notional=5.0)


class KagitBroker:
    """Sanal cüzdanlı broker. Durum SQLite'ta; yeniden başlatma unutturmaz."""

    def __init__(self, state: StateStore, baslangic_usdt: float,
                 market, kaldirac: float = 3.0,
                 bakiye_anahtari: str = "scalp_usdt") -> None:
        self.state = state
        self.market = market
        self.kaldirac = kaldirac
        self._bakiye_anahtari = bakiye_anahtari
        self._baslangic = baslangic_usdt
        self._emir_sayaci_anahtari = "scalp_emir_no"
        if not self.state.get_kv(bakiye_anahtari, ""):
            self.state.set_kv(bakiye_anahtari, str(baslangic_usdt))

    # ------------------------------------------------------------ yardımcılar
    def _emir_no(self) -> int:
        n = int(self.state.get_kv(self._emir_sayaci_anahtari, "0") or 0) + 1
        self.state.set_kv(self._emir_sayaci_anahtari, str(n))
        return n

    def _bakiye(self) -> float:
        return float(self.state.get_kv(self._bakiye_anahtari, str(self._baslangic)))

    def _bakiye_yaz(self, v: float) -> None:
        self.state.set_kv(self._bakiye_anahtari, str(v))

    def _fiyat(self, symbol: str) -> Optional[float]:
        try:
            return self.market.last_price(symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] fiyat alınamadı: %s", symbol, e)
            return None

    # --------------------------------------------- FuturesBroker ile aynı yüz
    def filtreler(self, symbol: str) -> VadeliFiltre:
        return VARSAYILAN_FILTRE

    def bakiye_usdt(self) -> float:
        return self._bakiye()

    def kaldirac_ayarla(self, symbol: str, kaldirac: int) -> bool:
        self.kaldirac = float(kaldirac)
        return True

    def tek_yon_modu_mu(self) -> bool:
        return True            # sanal cüzdanda sembol başına tek pozisyon

    def pozisyon(self, symbol: str) -> Optional[dict]:
        p = self.state.get_position(symbol)
        if p is None or p.qty <= 0:
            return None
        return {"symbol": symbol, "miktar": p.qty, "yon": p.side,
                "giris": p.entry_price, "kaldirac": self.kaldirac,
                "likidasyon": 0.0}

    def stop_var_mi(self, symbol: str) -> Optional[dict]:
        p = self.state.get_position(symbol)
        if p is None or not p.stop_order_id:
            return None
        return {"orderId": p.stop_order_id, "stopPrice": p.trailing_stop}

    def emir_iptal(self, symbol: str, emir_id: int) -> bool:
        return True            # sanal emir; iptal her zaman başarılı

    # ------------------------------------------------------------- işlem akışı
    def giris_ve_stop(self, symbol: str, yon: str, miktar: float,
                      stop_fiyat: float) -> Optional[dict]:
        """Pozisyon açar ve stopunu kurar — gerçek brokerla AYNI sözleşme.

        Kâğıtta stop kurulamaz diye bir hâl yok; yine de dönüş biçimi aynı
        tutuluyor ('acil' bayrağı dahil) ki çağıran katman iki broker için
        tek kod yolu kullansın.
        """
        fiyat = self._fiyat(symbol)
        if fiyat is None:
            return None
        f = self.filtreler(symbol)
        miktar = asagi_yuvarla(miktar, f.step_size)
        if miktar < f.min_qty or miktar * fiyat < f.min_notional:
            log.info("[%s] kâğıt: miktar filtreden geçmedi (%.6g)", symbol, miktar)
            return None

        giris = fiyat * (1 + SLIPPAGE if yon == "LONG" else 1 - SLIPPAGE)
        notional = miktar * giris
        marj = notional / max(self.kaldirac, 1.0)
        ucret = notional * TAKER_FEE
        if marj + ucret > self._bakiye():
            log.info("[%s] kâğıt: bakiye yetersiz (marj %.2f + ücret %.2f > %.2f)",
                     symbol, marj, ucret, self._bakiye())
            return None

        self._bakiye_yaz(self._bakiye() - marj - ucret)
        return {"giris": giris, "miktar": miktar,
                "stop_id": self._emir_no(),
                "marj": marj, "giris_ucreti": ucret, "acil": False}

    def stop_tasi(self, symbol: str, yon: str, eski_id: Optional[int],
                  yeni_stop: float) -> Optional[int]:
        """Yeni stop kimliği döner. Gerçek brokerda sıra önemliydi (önce yeni,
        sonra eskiyi iptal); kâğıtta korumasız an oluşmaz ama arayüz aynı."""
        f = self.filtreler(symbol)
        _ = fiyat_yuvarla(yeni_stop, f.tick_size)
        return self._emir_no()

    def pozisyonu_kapat(self, symbol: str, yon: str, miktar: float,
                        stop_id: Optional[int] = None) -> Optional[Dolum]:
        """Pozisyonun tamamını VEYA bir kısmını kapatır.

        KISMİ KAPATMADA stop_id GEÇİLMEMELİ: gerçek borsada stop emri
        `closePosition=true` olduğu için kalan miktarı korumaya devam eder;
        iptal edilirse kalan yarı STOPSUZ kalır. Aynı kuralı kâğıtta da
        uyguluyoruz ki davranış birebir olsun.
        """
        fiyat = self._fiyat(symbol)
        if fiyat is None:
            return None
        cikis = fiyat * (1 - SLIPPAGE if yon == "LONG" else 1 + SLIPPAGE)
        self._bakiye_yaz(self._bakiye() - miktar * cikis * TAKER_FEE)
        return Dolum(ort_fiyat=cikis, miktar=miktar, emir_id=self._emir_no())

    # ------------------------------------------------- kâğıda özgü muhasebe
    def marj_ve_kar_geri_yaz(self, marj: float, realize: float) -> None:
        """Pozisyon kapanınca bloke marjı ve realize K/Z'yi bakiyeye döndürür.

        Gerçek brokerda bunu borsa yapar; kâğıtta bizim yapmamız gerekir.
        Trader bu yüzden broker tipini sormak zorunda kalmasın diye ayrı
        isimde: gerçek brokerda bu metot YOKTUR ve çağrılmaz (hasattr).
        """
        self._bakiye_yaz(self._bakiye() + marj + realize)
