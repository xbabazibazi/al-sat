"""SCALP KANALI — kısa süreli, kaldıraçlı gir-çık. Ana kanaldan TAM YALITIM.

Ana kanal 4 saatlik mumla gün/hafta tutar. Bu kanal 15 dakikalık mumla
saatler tutar. İkisi aynı veritabanını, cüzdanı ve istatistiği PAYLAŞMAZ —
karışırsa hangisinin para kazandığı ölçülemez hâle gelir.

İKİ PARAMETRE (kullanıcının şartı: "1 veya duruma göre 2 parametre"):
  1. Donchian kırılımı → ZAMANLAMA. Projede en çok sınanmış tetik.
  2. ATR              → STOP MESAFESİ ve POZİSYON BOYUTU.
Bilinçli olarak YOK: EMA200 trend filtresi, RSI, 5 araçlı ön değerlendirme.
Amaç saf iki parametrenin ne ürettiğini ölçmek; filtre eklemek sonra,
veriyle olur. (Bu yüzden `check_entry` kullanılmıyor — o üçünü paketliyor.)

MALİYET KAPISI — bu kanalın yaşam savaşı:
  2026-10-01 ölçümü (canlı Binance, 5 parite, son 100 mum):
      1dk ATR %0.111 · 5dk %0.208 · 15dk %0.458 · 1sa %1.056
  Gidiş-dönüş sürtünme ≈ %0.20 (taker %0.05×2 + kayma %0.05×2).
  Yani 1 dakikada sürtünme 1R hedefin %135'i — matematiksel olarak kayıp.
  Projenin kendi notu da bunu söylüyordu: "1h backtestte komisyona yenildi".
  Bu yüzden her girişte HEDEF, sürtünmenin en az N katı olmak zorunda;
  değilse işleme GİRİLMEZ. Sessiz sessiz komisyon bağışlamayı yasaklayan
  kapı budur.

ÇIKIŞ (kullanıcının iki isteği birlikte):
  • +1.5R'de pozisyonun YARISI kapanır → kâr gerçekten cebe girer.
  • Kalan yarı iz süren stopla devam eder, stop ASLA geri çekilmez.
  • Yarı alındıktan sonra stop en az GİRİŞE çekilir: koşan yarı artık
    zarar edemez.

BROKER-AGNOSTİK: kâğıt (KagitBroker) ile gerçek borsa (FuturesBroker) aynı
arayüzü uygular; bu dosya hangisiyle çalıştığını bilmez. Canlıya geçiş
konfig değişikliğidir, kod değişikliği değil.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import Config
from .notifier import TelegramNotifier
from .state import Position, StateStore
from .strategy import compute_indicators

log = logging.getLogger("scalp")

# Yarı kâr alındı mı — pozisyon başına bayrak (Position dataclass'ına alan
# eklemek iki kanalı birden etkilerdi; bu yüzden kv'de tutuluyor).
YARI_ONEK = "syari_"


class ScalpTrader:
    def __init__(self, symbol: str, cfg: Config, market, state: StateStore,
                 broker, notifier: TelegramNotifier) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.market = market
        self.state = state
        self.broker = broker
        self.notifier = notifier
        self._kaldirac_ayarlandi = False
        self._mum_onbellek: tuple[float, object] | None = None

    # ------------------------------------------------------------ yardımcılar
    def _bugun(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _sayac(self, ad: str) -> float:
        return float(self.state.get_kv(f"s{ad}_{self._bugun()}", "0") or 0)

    def _sayac_arttir(self, ad: str, miktar: float = 1.0) -> None:
        self.state.set_kv(f"s{ad}_{self._bugun()}",
                          str(self._sayac(ad) + miktar))

    def _yari_alindi(self) -> bool:
        return self.state.get_kv(f"{YARI_ONEK}{self.symbol}", "") == "1"

    def _yari_isaretle(self, deger: bool) -> None:
        self.state.set_kv(f"{YARI_ONEK}{self.symbol}", "1" if deger else "")

    def _mumlar(self):
        """15 dakikalık mumlar — kısa ömürlü önbellekle.

        Tur 20 saniyede bir dönüyor ama 15dk mumu 15 dakikada bir değişiyor;
        her turda 300 mum çekmek saf israf. 20 parite ile bu, dakikada 60
        gereksiz istek demekti. TTL kısa tutuldu (30 sn) ki yeni mum en fazla
        yarım dakika gecikmeyle görülsün — giriş kararı bundan etkilenmesin.
        FİYAT önbelleğe ALINMAZ: stop kontrolü taze fiyat ister.
        """
        import time as _t
        simdi = _t.time()
        if self._mum_onbellek and simdi - self._mum_onbellek[0] < 30:
            return self._mum_onbellek[1]
        df = self.market.klines(self.symbol, self.cfg.scalp_timeframe, limit=300)
        self._mum_onbellek = (simdi, df)
        return df

    def _sonuc(self, durum: str, mesaj: str) -> None:
        """Manuel komutun âkıbeti — panel okur. Sessiz yutma yasak."""
        self.state.set_kv(
            f"scmdres_{self.symbol}",
            f"{durum}|{datetime.now(timezone.utc).isoformat()}|{mesaj}")

    # ------------------------------------------------------- maliyet kapısı
    def surtunme_pct(self) -> float:
        """Gidiş-dönüş sürtünme, fiyatın yüzdesi olarak."""
        return (self.cfg.scalp_taker_fee + self.cfg.scalp_slippage) * 2 * 100.0

    def maliyet_kapisi(self, atr_pct: float) -> tuple[bool, str]:
        """Hedef, sürtünmenin yeterli katı mı? Değilse giriş YOK.

        Hedef = kar_hedefi_r × atr_carpani × ATR. Sürtünme sabit olduğu için
        küçük ATR'de hedefin tamamını komisyon yer. Bu kapı, 1 saatlik modu
        öldüren hatayı tekrarlamamızı engelliyor.
        """
        hedef_pct = self.cfg.scalp_kar_hedefi_r * self.cfg.scalp_atr_carpani * atr_pct
        s = self.surtunme_pct()
        kat = hedef_pct / s if s > 0 else 0.0
        if kat < self.cfg.scalp_min_hedef_kat:
            return False, (f"maliyet kapısı: hedef %{hedef_pct:.3f} = sürtünmenin "
                           f"{kat:.1f} katı (en az {self.cfg.scalp_min_hedef_kat:g} şart)")
        return True, f"hedef %{hedef_pct:.3f} = sürtünmenin {kat:.1f} katı"

    # ------------------------------------------------------------- frenler
    def girisler_acik_mi(self) -> tuple[bool, str]:
        """Frenler. 0 = o fren KAPALI (kâğıtta ölçümü sansürlememek için).

        İki tür sınır var ve karıştırılmamalı:
          • SANSÜRLEYEN frenler (günlük zarar, zarar serisi): devreye girince
            kötü günlerin kuyruğu ölçümden silinir ve karne olduğundan iyi
            görünür. Kâğıtta kapalı; canlıda zorunlu (config.validate).
          • YAPISAL sınırlar (işlem tavanı, pozisyon tavanı): ölçümü
            bozmazlar. İşlem tavanı kaçak döngü tamponu, pozisyon tavanı ise
            marjın fiziken yetmesi için gerekli. İkisi her modda açık.
        """
        seri_esik = self.cfg.scalp_max_zarar_serisi
        if seri_esik > 0 and self._sayac("zarar_serisi") >= seri_esik:
            return False, (f"{seri_esik} üst üste zarar — "
                           f"bugün yeni giriş yok (rejim değişmiş olabilir)")
        if self._sayac("islem") >= self.cfg.scalp_max_gunluk_islem:
            return False, (f"günlük işlem tavanı ({self.cfg.scalp_max_gunluk_islem}) "
                           f"doldu — bu bir kaçak döngü tamponudur, normalde dolmaz")
        zarar_esik = self.cfg.scalp_max_gunluk_zarar
        if zarar_esik > 0:
            gunluk = self._sayac("pnl")
            varlik = self.varlik()
            if varlik > 0 and gunluk < 0 and abs(gunluk) / varlik >= zarar_esik:
                return False, (f"günlük zarar sınırı %{zarar_esik * 100:g} "
                               f"aşıldı ({gunluk:+.2f} USDT)")
        if len(self.state.all_positions()) >= self.cfg.scalp_max_pozisyon:
            return False, f"eşzamanlı pozisyon tavanı ({self.cfg.scalp_max_pozisyon})"
        return True, ""

    def varlik(self) -> float:
        """Bakiye + açık pozisyonların marjı. Kâğıt ve canlıda aynı anlam."""
        try:
            v = self.broker.bakiye_usdt()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] bakiye okunamadı: %s", self.symbol, e)
            return 0.0
        for p in self.state.all_positions():
            v += p.margin
        return v

    # --------------------------------------------------------------- kapanış
    def _kapat(self, pos: Position, cikis: float, oran: float, sebep: str) -> None:
        """Pozisyonun `oran` kadarını kapatır (1.0 = tamamı).

        KISMİ KAPATMADA stop_id GEÇİLMEZ: borsadaki stop `closePosition=true`
        olduğu için kalan miktarı korumaya devam eder. İptal edilseydi kalan
        yarı STOPSUZ kalırdı — kullanıcının değişmez şartına aykırı.
        """
        miktar = pos.qty * oran
        tam = oran >= 0.999
        # `cikis` DOLUM FİYATI olarak geçirilir. Stop çıkışında bu STOP
        # SEVİYESİdir: gerçek borsada STOP_MARKET fiyat seviyeye dokunduğu an
        # tetikleniyor, 20 saniye sonraki piyasa fiyatından değil. Eskiden
        # parametre yok sayılıyordu ve kâğıt kaybı ~2 katına çıkıyordu.
        ek = {}
        if cikis and hasattr(self.broker, "marj_ve_kar_geri_yaz"):
            ek["fiyat"] = cikis      # yalnız kâğıt broker dolum fiyatı kabul eder
        dolum = self.broker.pozisyonu_kapat(
            self.symbol, pos.side, miktar,
            stop_id=pos.stop_order_id if tam else None, **ek)
        if dolum is None:
            log.error("[%s] kapatma başarısız (oran %.2f) — pozisyon duruyor",
                      self.symbol, oran)
            self._sonuc("red", f"Kapatma başarısız ({sebep}) — pozisyon duruyor")
            return
        gercek_cikis = dolum.ort_fiyat

        yon = 1 if pos.side == "LONG" else -1
        brut = (gercek_cikis - pos.entry_price) * yon * dolum.miktar
        ucret_payi = pos.entry_fee_usdt * oran
        net = brut - ucret_payi - dolum.miktar * gercek_cikis * self.cfg.scalp_taker_fee
        marj_payi = pos.margin * oran

        # Kâğıt broker'da marjı ve K/Z'yi bakiyeye biz döndürürüz; gerçek
        # borsada bunu Binance yapar. Tip sormak yerine yeteneğe bakıyoruz.
        if hasattr(self.broker, "marj_ve_kar_geri_yaz"):
            self.broker.marj_ve_kar_geri_yaz(marj_payi, net)

        now = datetime.now(timezone.utc).isoformat()
        self.state.record_trade(
            self.symbol, pos.entry_time, now, pos.entry_price, gercek_cikis,
            dolum.miktar, f"{sebep} · {'kâr kilitlendi' if net >= 0 else 'zarar kesildi'}",
            side=pos.side, pnl_override=net)
        self._sayac_arttir("pnl", net)
        self._sayac_arttir("islem")

        r = (net / (pos.risk_unit * dolum.miktar)) if pos.risk_unit > 0 else 0.0
        if tam:
            self.state.clear_position(self.symbol)
            self._yari_isaretle(False)
            # Zarar serisi yalnızca TAM kapanışta sayılır: yarı kâr alıp
            # kalanı başabaşta kapanan işlem "zarar" değildir.
            if net < 0:
                self._sayac_arttir("zarar_serisi")
            else:
                self.state.set_kv(f"szarar_serisi_{self._bugun()}", "0")
        else:
            pos.qty -= dolum.miktar
            pos.margin -= marj_payi
            pos.entry_fee_usdt -= ucret_payi
            self.state.save_position(pos)

        emoji = "🟢" if net >= 0 else "🔴"
        pay = "tamamı" if tam else f"%{oran * 100:.0f}'ı"
        self.notifier.send(
            f"{emoji} *SCALP · {self.symbol} {pos.side}* — {pay} kapandı\n"
            f"• Sebep: {sebep}\n"
            f"• Giriş `{pos.entry_price:,.6g}` → Çıkış `{gercek_cikis:,.6g}`\n"
            f"• Net `{net:+,.2f}` USDT · `{r:+.2f}R`"
            + ("" if tam else f"\n• Kalan `{pos.qty:,.6g}` iz süren stopla devam ediyor"))
        log.info("[%s] scalp %s kapandı (%s): net %+.2f USDT (%.2fR)",
                 self.symbol, pay, sebep, net, r)

    # ----------------------------------------------------------------- giriş
    def _ac(self, yon: str, fiyat: float, atr: float, manuel: bool = False) -> None:
        acik, sebep = (True, "") if manuel else self.girisler_acik_mi()
        if not acik:
            log.info("[%s] giriş engellendi: %s", self.symbol, sebep)
            if manuel:
                self._sonuc("red", sebep)
            return

        risk_mesafe = atr * self.cfg.scalp_atr_carpani
        if risk_mesafe <= 0:
            return
        varlik = self.varlik()
        risk_usdt = varlik * self.cfg.scalp_risk_pct
        miktar = risk_usdt / risk_mesafe

        # NOTIONAL TAVANI: dar stop + sabit %risk, farkında olmadan çok büyük
        # bir pozisyon üretir (risk/mesafe bölmesi küçük bölen demek). Tek bir
        # scalp hesabın tamamını kilitlemesin.
        tavan = varlik * self.cfg.scalp_notional_tavani
        if miktar * fiyat > tavan:
            miktar = tavan / fiyat
            log.info("[%s] miktar notional tavanına kısıldı", self.symbol)

        stop = fiyat - risk_mesafe if yon == "LONG" else fiyat + risk_mesafe
        if not self._kaldirac_ayarlandi:
            try:
                self.broker.kaldirac_ayarla(self.symbol, int(self.cfg.scalp_kaldirac))
                self._kaldirac_ayarlandi = True
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] kaldıraç ayarlanamadı: %s", self.symbol, e)

        sonuc = self.broker.giris_ve_stop(self.symbol, yon, miktar, stop)
        if sonuc is None:
            log.info("[%s] scalp girişi açılamadı", self.symbol)
            if manuel:
                self._sonuc("red", "Giriş açılamadı (bakiye/filtre/stop)")
            return
        if sonuc.get("acil"):
            # Pozisyon açık ama STOPSUZ ve kapatılamadı. Bu bir acil durumdur.
            self.notifier.send_error(
                f"🆘 SCALP ACİL — {self.symbol} {yon} pozisyon AÇIK ama STOP YOK "
                f"ve kapatılamadı. Derhal Binance'ten elle kapatın.")
            return

        giris = sonuc["giris"]
        pos = Position(
            symbol=self.symbol, qty=sonuc["miktar"], entry_price=giris,
            highest_price=giris,
            trailing_stop=giris - risk_mesafe if yon == "LONG" else giris + risk_mesafe,
            stop_order_id=sonuc.get("stop_id"),
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_fee_usdt=sonuc.get("giris_ucreti", 0.0), side=yon,
            margin=sonuc.get("marj", 0.0), risk_unit=risk_mesafe)
        self.state.save_position(pos)
        self._yari_isaretle(False)

        hedef = (giris + self.cfg.scalp_kar_hedefi_r * risk_mesafe if yon == "LONG"
                 else giris - self.cfg.scalp_kar_hedefi_r * risk_mesafe)
        self.notifier.send(
            f"⚡ *SCALP {yon} açıldı* · `{self.symbol}`\n"
            f"• Giriş `{giris:,.6g}` · miktar `{pos.qty:,.6g}`\n"
            f"• Stop `{pos.trailing_stop:,.6g}` (1R = `{risk_mesafe:,.6g}`)\n"
            f"• Hedef `{hedef:,.6g}` → yarısı burada kapanır "
            f"({self.cfg.scalp_kar_hedefi_r:g}R)")
        log.info("[%s] SCALP %s açıldı @ %.6g stop %.6g", self.symbol, yon,
                 giris, pos.trailing_stop)
        if manuel:
            self._sonuc("ok", f"{yon} açıldı @ {giris:,.6g}")

    # ----------------------------------------------------------- iz süren stop
    def _stop_guncelle(self, pos: Position, fiyat: float, atr: float) -> Position:
        """Cırcır: stop yalnızca lehe hareket eder, ASLA geri çekilmez."""
        if pos.stop_manual_ref > 0:
            return pos          # manuel gevşetme kilidi açık — dokunma
        if pos.side == "LONG":
            pos.highest_price = max(pos.highest_price, fiyat)
            aday = pos.highest_price - atr * self.cfg.scalp_atr_carpani
            # Yarı kâr alındıysa stop en az GİRİŞE çekilir: koşan yarı
            # artık zarar edemez. Kullanıcının "stopu yukarı alsın" isteği.
            if self._yari_alindi():
                aday = max(aday, pos.entry_price)
            yeni = max(pos.trailing_stop, aday)
        else:
            pos.highest_price = min(pos.highest_price, fiyat)
            aday = pos.highest_price + atr * self.cfg.scalp_atr_carpani
            if self._yari_alindi():
                aday = min(aday, pos.entry_price)
            yeni = min(pos.trailing_stop, aday)

        if abs(yeni - pos.trailing_stop) > 1e-12:
            yeni_id = self.broker.stop_tasi(self.symbol, pos.side,
                                            pos.stop_order_id, yeni)
            if yeni_id is None:
                # Yeni stop kurulamadı: ESKİSİ KORUNUR. Korumasız an yok.
                log.warning("[%s] stop taşınamadı, eski stop korunuyor", self.symbol)
                return pos
            pos.trailing_stop = yeni
            pos.stop_order_id = yeni_id
        self.state.save_position(pos)
        return pos

    def _stop_tetiklendi(self, pos: Position, fiyat: float) -> bool:
        return (fiyat <= pos.trailing_stop if pos.side == "LONG"
                else fiyat >= pos.trailing_stop)

    def _hedefe_vardi(self, pos: Position, fiyat: float) -> bool:
        if pos.risk_unit <= 0:
            return False
        kazanc = ((fiyat - pos.entry_price) if pos.side == "LONG"
                  else (pos.entry_price - fiyat))
        return kazanc >= self.cfg.scalp_kar_hedefi_r * pos.risk_unit

    # -------------------------------------------------------- manuel komutlar
    def _manuel_komutlar(self, pos: Position | None, fiyat: float) -> Position | None:
        """Panel/Telegram komutları. Tek yazıcı ilkesi: komut kuyrukta, uygulama burada."""
        anahtar = f"scmd_{self.symbol}"
        cmd = self.state.get_kv(anahtar, "")
        if not cmd:
            return pos
        self.state.set_kv(anahtar, "")

        if cmd == "CLOSE":
            if pos is None:
                self._sonuc("red", "Kapatılacak açık pozisyon yok.")
                return None
            self._kapat(pos, fiyat, 1.0, "manuel kapatma")
            self._sonuc("ok", f"Pozisyon kapatıldı @ {fiyat:,.6g}")
            return None

        if cmd.startswith("STOP:"):
            if pos is None:
                self._sonuc("red", "Stop değiştirilecek açık pozisyon yok.")
                return pos
            try:
                yeni = float(cmd[5:])
            except ValueError:
                self._sonuc("red", f"Stop sayıya çevrilemedi: {cmd[5:]!r}")
                return pos
            if yeni <= 0:
                self._sonuc("red", "Stop 0 veya negatif olamaz.")
                return pos
            # Anında tetikleyecek seviye reddedilir — kullanıcı KAPAT demek
            # istiyorsa onu açıkça söylemeli.
            if (pos.side == "LONG" and yeni >= fiyat) or (pos.side == "SHORT" and yeni <= fiyat):
                self._sonuc("red", f"{yeni:,.6g} anlık fiyatın ({fiyat:,.6g}) yanlış "
                                   f"tarafında — anında tetiklenirdi. KAPAT kullanın.")
                return pos
            eski = pos.trailing_stop
            gevsiyor = (yeni < eski) if pos.side == "LONG" else (yeni > eski)
            yeni_id = self.broker.stop_tasi(self.symbol, pos.side, pos.stop_order_id, yeni)
            if yeni_id is None:
                self._sonuc("red", "Borsada stop taşınamadı — eski stop duruyor.")
                return pos
            pos.trailing_stop = yeni
            pos.stop_order_id = yeni_id
            # Gevşetildiyse kilit kur: cırcır bir sonraki turda geri almasın.
            # Kilit, işlem verilen nefes payını geri kazanınca kendiliğinden açılır.
            pos.stop_manual_ref = (2 * eski - yeni) if gevsiyor else 0.0
            self.state.save_position(pos)
            self._sonuc("ok", f"Stop {'GEVŞETİLDİ' if gevsiyor else 'sıkıldı'}: "
                              f"{eski:,.6g} → {yeni:,.6g}")
            return pos

        if cmd in ("OPEN_LONG", "OPEN_SHORT"):
            if pos is not None:
                self._sonuc("red", f"Zaten açık {pos.side} pozisyon var.")
                return pos
            df = self.market.klines(self.symbol, self.cfg.scalp_timeframe, limit=120)
            ind = compute_indicators(df.iloc[:-1], self.cfg.strategy)
            atr = float(ind.iloc[-1]["atr"])
            self._ac("LONG" if cmd == "OPEN_LONG" else "SHORT", fiyat, atr, manuel=True)
            return self.state.get_position(self.symbol)

        self._sonuc("red", f"Bilinmeyen komut: {cmd}")
        return pos

    # ------------------------------------------------------------- ana döngü
    def poll(self) -> None:
        fiyat = self.market.last_price(self.symbol)
        if fiyat is None:
            return
        self.state.set_kv(f"sfiyat_{self.symbol}",
                          f"{fiyat}|{datetime.now(timezone.utc).isoformat()}")

        pos = self.state.get_position(self.symbol)
        pos = self._manuel_komutlar(pos, fiyat)

        df = self._mumlar()
        kapanmis = df.iloc[:-1]
        if len(kapanmis) < 60:
            return
        ind = compute_indicators(kapanmis, self.cfg.strategy)
        son = ind.iloc[-1]
        atr = float(son["atr"])
        if atr <= 0:
            return

        # 1) Açık pozisyon yönetimi — her poll, mum beklemeden.
        if pos is not None:
            if self._stop_tetiklendi(pos, fiyat):
                # STOP SEVİYESİNDEN kapanır, o anki fiyattan değil: borsadaki
                # STOP_MARKET seviyeye dokunduğu an tetiklenir. Ana kanal da
                # aynısını yapıyor (futures_trader._close(pos.trailing_stop*slip)).
                self._kapat(pos, pos.trailing_stop, 1.0, "izleyen stop")
                return
            if not self._yari_alindi() and self._hedefe_vardi(pos, fiyat):
                self._kapat(pos, fiyat, 0.5,
                            f"kâr hedefi {self.cfg.scalp_kar_hedefi_r:g}R")
                self._yari_isaretle(True)
                pos = self.state.get_position(self.symbol)
                if pos is None:
                    return
            self._stop_guncelle(pos, fiyat, atr)
            return

        # 2) Yeni giriş — yalnızca YENİ KAPANMIŞ mumda (aynı mumda tekrar girmeyelim).
        son_mum = int(kapanmis.iloc[-1]["open_time"])
        if son_mum == self.state.get_last_candle(self.symbol):
            return
        self.state.set_last_candle(self.symbol, son_mum)

        # MALİYET KAPISI — stratejiden ÖNCE. Sinyal ne kadar güzel olsa da
        # hedef komisyonu karşılamıyorsa girilmez.
        atr_pct = atr / fiyat * 100.0
        gecer, aciklama = self.maliyet_kapisi(atr_pct)
        self.state.save_assessment(self.symbol, datetime.now(timezone.utc).isoformat(),
                                   f'{{"atr_pct": {atr_pct:.4f}, "kapi": "{aciklama}"}}')
        if not gecer:
            log.debug("[%s] %s", self.symbol, aciklama)
            return

        kapanis = float(son["close"])
        ust, alt = son.get("donchian_high"), son.get("donchian_low")
        yon = None
        if ust is not None and ust == ust and kapanis > float(ust):
            yon = "LONG"
        elif (self.cfg.scalp_allow_short and alt is not None and alt == alt
              and kapanis < float(alt)):
            yon = "SHORT"
        if yon is None:
            return

        acik, sebep = self.girisler_acik_mi()
        if not acik:
            log.info("[%s] sinyal var ama fren devrede: %s", self.symbol, sebep)
            return
        log.info("[%s] SCALP sinyali: %s kırılımı · %s", self.symbol, yon, aciklama)
        self._ac(yon, fiyat, atr)
