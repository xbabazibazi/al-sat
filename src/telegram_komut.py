"""TELEGRAM KOMUT KATMANI — panelin yaptığını telefondan yapabilmek.

    /durum            anlık tablo (kripto + borsa, canlı fiyatla)
    /kapat  SEMBOL    pozisyonu kapat        → ONAY ister
    /stop   SEMBOL F  stop seviyesini değiştir → ONAY ister
    /onay   KOD       bekleyen işlemi onayla
    /iptal            bekleyen işlemi iptal et
    /yardim           komut listesi

TASARIM KARARLARI — her biri bir riski kapatıyor:

1) TEK YAZICI İLKESİ KORUNUR. Bu modül pozisyona ASLA dokunmaz; tıpkı panel
   gibi komutu SQLite kuyruğuna (cmd_/bcmd_) yazar, uygulamayı bot yapar.
   Böylece pozisyon/bakiye üzerinde tek yazıcı bot kalmaya devam eder.
   Yan faydası: Telegram'dan gelen komut da panelin tüm güvenlik denetimlerine
   (anlık fiyatın ötesinde stop reddi, gevşetme kilidi...) aynen tabidir.

2) YETKİ TEK KİŞİDE. Yalnızca .env'deki TELEGRAM_CHAT_ID komut verebilir.
   Bot linkini bilen herkes yazabilir — yabancı mesaj ÇALIŞTIRILMAZ, sahibe
   bir kez haber verilir (her mesajda değil; spam sel olurdu).

3) YIKICI İŞ ONAY İSTER. "kapat" ve "stop" geri alınamaz sonuç doğurur.
   Bot 4 haneli kod üretir, kullanıcı /onay KOD yazar. Neden düz "evet"
   değil: yanlışlıkla yukarı kaydırıp eski mesajı tekrar göndermek ya da
   başka bir sembolün onayını bu sembole uygulamak mümkün olmasın. Kod
   sembole ve eyleme bağlıdır ve 2 dakikada düşer.

4) YENİDEN BAŞLAMA ESKİ KOMUTU DİRİLTMEZ. Telegram okunmamış mesajları 24
   saat saklar. Offset kaydedilmeseydi bot her açılışta dünkü "/kapat"ı
   yeniden işlerdi. Offset kv'de tutulur; ilk kurulumda kuyruk BOŞALTILIR
   (çalıştırılmadan atlanır).
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone

import requests

from .config import Config
from .performans import ozet
from .state import StateStore

log = logging.getLogger("tgkomut")

API = "https://api.telegram.org/bot{token}/{metot}"
OFFSET_ANAHTARI = "tg_offset"
BEKLEYEN_TTL_S = 120          # onay penceresi
UZUN_BEKLEME_S = 50           # getUpdates long-polling süresi
SIMGE = {"USD": "$", "TRY": "₺"}

YARDIM = (
    "*AL-SAT komutları*\n"
    "`/durum` — anlık tablo (kripto + borsa)\n"
    "`/kapat` — açık pozisyonları düğme olarak listeler, seç ve onayla\n"
    "`/kapat SEMBOL` — doğrudan o pozisyonu hedefler\n"
    "`/stop SEMBOL FİYAT` — stop seviyesini istediğin rakama çeker\n"
    "`/onay KOD` · `/iptal` — düğme çalışmazsa elle onay / vazgeçme\n"
    "`/yardim` — bu liste\n\n"
    "_Komutlar panelle aynı kuyruğa düşer; uygulamayı bot yapar "
    "(~1 dk). Reddedilirse sebebini yazar._"
)

# TELEGRAM MENÜSÜ. Bunu kaydetmezsek sohbette "/" yazınca HİÇBİR ŞEY
# çıkmaz ve komutlar ezberden yazılmak zorunda kalır — kullanıcı da haklı
# olarak "komut yok galiba" diye okur (2026-09-18'de tam bu oldu).
# Var olan bir yeteneğin görünmez olması, olmamasıyla aynı kapıya çıkıyor.
MENU = [
    {"command": "durum", "description": "Anlık tablo — pozisyonlar, K/Z, stop, karne"},
    {"command": "kapat", "description": "Pozisyon kapat — yazınca liste çıkar, seç ve onayla"},
    {"command": "stop", "description": "Stop'u istediğin rakama çek — /stop SEMBOL FİYAT"},
    {"command": "onay", "description": "Bekleyen işlemi onayla — /onay KOD"},
    {"command": "iptal", "description": "Bekleyen işlemden vazgeç"},
    {"command": "yardim", "description": "Komut listesi"},
]


class TelegramKomut:
    """Telegram'ı dinler, yetkiyi denetler, komutu kuyruğa bırakır."""

    def __init__(self, cfg: Config, state: StateStore,
                 borsa_state: StateStore | None, market) -> None:
        self.cfg = cfg
        self.state = state
        self.borsa_state = borsa_state
        self.market = market
        self.sahip = str(cfg.telegram_chat_id or "").strip()
        self._bekleyen: dict | None = None      # {kod, tur, sembol, fiyat, son}
        self._yabanci_uyarildi: set[str] = set()

    # ------------------------------------------------------------- alt seviye
    def _cagir(self, metot: str, **veri):
        try:
            r = requests.post(API.format(token=self.cfg.telegram_token, metot=metot),
                              json=veri, timeout=UZUN_BEKLEME_S + 15)
            govde = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram %s başarısız: %s", metot, e)
            return None
        if not govde.get("ok"):
            # Telegram'ın KENDİ açıklamasını yaz; "başarısız" demek yetmiyor,
            # bu projede tam da o yüzden bir arıza günlerce görünmez kaldı.
            log.error("Telegram %s reddetti: %s", metot, govde.get("description"))
            return None
        return govde.get("result")

    def _yaz(self, chat_id, metin: str, dugmeler: dict | None = None) -> None:
        veri = {"chat_id": chat_id, "text": metin, "parse_mode": "Markdown",
                "disable_web_page_preview": True}
        if dugmeler:
            veri["reply_markup"] = dugmeler
        self._cagir("sendMessage", **veri)

    @staticmethod
    def _onay_dugmeleri(kod: str) -> dict:
        """Onay/vazgeç düğmeleri — kod düğmenin İÇİNE gömülür.

        Kodu elle yazdırmak koruma sağlıyordu ama zahmetliydi. Düğme aynı
        korumayı tek dokunuşla verir: callback_data o mesaja ait kodu taşır,
        kod kullanılınca tükenir, eski bir mesajın düğmesine basmak bu yüzden
        iş görmez. Yani kolaylık koruma pahasına değil.
        """
        return {"inline_keyboard": [[
            {"text": "✅ ONAYLA", "callback_data": f"onay:{kod}"},
            {"text": "🚫 Vazgeç", "callback_data": "iptal"},
        ]]}

    # ------------------------------------------------------------- /durum
    def _fiyat(self, sym: str) -> float | None:
        try:
            return self.market.last_price(sym)
        except Exception:  # noqa: BLE001
            return None

    def _kripto_blok(self) -> str:
        bakiye = float(self.state.get_kv("fut_usdt", "10000.0"))
        varlik = bakiye
        satirlar = []
        for p in self.state.all_positions():
            fiyat = self._fiyat(p.symbol)
            if fiyat is None:
                satirlar.append(f"• `{p.symbol}` {p.side} — fiyat okunamadı")
                continue
            upnl = (p.qty * (fiyat - p.entry_price) if p.side == "LONG"
                    else p.qty * (p.entry_price - fiyat)) - p.funding_acc
            varlik += p.margin + upnl
            r = (((fiyat - p.entry_price) if p.side == "LONG"
                  else (p.entry_price - fiyat)) / p.risk_unit) if p.risk_unit > 0 else None
            # ŞİMDİ STOP OLURSA BANKAYA GİRECEK OLAN. "Anlık kâr" ile aynı şey
            # DEĞİL: anlık kâr fiyat oradayken kâğıt üstündeki değer, bu ise
            # stop tetiklenirse gerçekten cebe girecek/çıkacak tutar. Kullanıcı
            # kararını buna göre veriyor, o yüzden ayrı satırda ve açık yazılır.
            stop_pnl = p.qty * ((p.trailing_stop - p.entry_price) if p.side == "LONG"
                                else (p.entry_price - p.trailing_stop))
            kilit = "🔒 kilitli kâr" if stop_pnl >= 0 else "göze alınan zarar"
            satirlar.append(
                f"• `{p.symbol}` {p.side} `{fiyat:,.6g}`\n"
                f"   anlık `{upnl:+,.2f}` USDT" +
                (f" · `{r:+.2f}R`" if r is not None else "") + "\n"
                f"   stop `{p.trailing_stop:,.6g}` → *şimdi stop olursa* "
                f"`{stop_pnl:+,.2f}` USDT _({kilit})_"
            )
        o = ozet(self.state.pnl_sirali())
        bas = (f"*KRİPTO* — varlık `{varlik:,.2f}` USDT "
               f"(`{varlik - 10000:+,.2f}`)\n"
               f"serbest `{bakiye:,.2f}` · açık {len(satirlar)}\n")
        karne = (f"karne: {o['n']} işlem · kazanma %{o['kazanma_orani']} · "
                 f"ödeme {o['odeme_orani']:.2f}× · beklenti `{o['beklenti']:+.2f}`/işlem")
        return bas + ("\n".join(satirlar) if satirlar else "_açık pozisyon yok_") + "\n" + karne

    def _borsa_blok(self) -> str:
        if self.borsa_state is None:
            return ""
        satirlar = []
        for p in self.borsa_state.all_positions():
            cur = "TRY" if p.symbol.endswith(".IS") else "USD"
            s = SIMGE[cur]
            ham = self.borsa_state.get_kv(f"bfiyat_{p.symbol}", "")
            fiyat, _, ts = ham.partition("|")
            try:
                f = float(fiyat)
            except ValueError:
                satirlar.append(f"• `{p.symbol}` — fiyat yok")
                continue
            upnl = (p.qty * (f - p.entry_price) if p.side == "LONG"
                    else p.qty * (p.entry_price - f))
            # Fiyatın YAŞI yazılır: borsa verisi gecikmeli ve piyasa kapalıyken
            # donar. Bayat fiyatı taze göstermek, yanlış karar aldırır.
            yas = ""
            try:
                yas_dk = (datetime.now(timezone.utc)
                          - datetime.fromisoformat(ts)).total_seconds() / 60
                if yas_dk > 20:
                    yas = f" _({yas_dk:.0f} dk önce)_"
            except Exception:  # noqa: BLE001
                pass
            # Kripto tarafıyla aynı bilgi: stop şimdi tetiklenirse ne olacak.
            # Burada hiç yoktu — aynı soruya iki kanalda farklı cevap vermek
            # kullanıcıyı kanal değiştirdiğinde kör bırakıyordu.
            stop_pnl = p.qty * ((p.trailing_stop - p.entry_price) if p.side == "LONG"
                                else (p.entry_price - p.trailing_stop))
            kilit = "🔒 kilitli kâr" if stop_pnl >= 0 else "göze alınan zarar"
            satirlar.append(
                f"• `{p.symbol}` {p.side} `{s}{f:,.2f}`{yas}\n"
                f"   anlık `{upnl:+,.2f} {cur}`\n"
                f"   stop `{s}{p.trailing_stop:,.2f}` → *şimdi stop olursa* "
                f"`{stop_pnl:+,.2f} {cur}` _({kilit})_")
        return "\n\n*BORSA* (sanal cüzdan)\n" + (
            "\n".join(satirlar) if satirlar else "_açık pozisyon yok_")

    def _durum(self) -> str:
        simdi = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
        return f"📊 *DURUM* · {simdi}\n\n" + self._kripto_blok() + self._borsa_blok()

    # ------------------------------------------------- yıkıcı komutlar + onay
    def _hedef_bul(self, sembol: str):
        """Sembol hangi kanala ait? (durum_deposu, komut_ön_eki) döndürür."""
        sembol = sembol.upper()
        if sembol in self.cfg.symbols:
            return self.state, "cmd_", sembol
        if self.borsa_state is not None and sembol in self.cfg.borsa_symbols:
            return self.borsa_state, "bcmd_", sembol
        return None, None, sembol

    def _bekleyeni_kur(self, tur: str, sembol: str, fiyat: float | None, ozet_metin: str) -> str:
        kod = f"{secrets.randbelow(9000) + 1000}"
        self._bekleyen = {"kod": kod, "tur": tur, "sembol": sembol,
                          "fiyat": fiyat, "son": time.time() + BEKLEYEN_TTL_S}
        return (f"{ozet_metin}\n\n"
                f"_{BEKLEYEN_TTL_S // 60} dakika geçerli. Düğme çalışmazsa "
                f"elle: _`/onay {kod}`")

    def _kapat_iste(self, sembol: str) -> str:
        depo, _, sembol = self._hedef_bul(sembol)
        if depo is None:
            return f"❓ `{sembol}` takip listesinde yok."
        pos = depo.get_position(sembol)
        if pos is None:
            return f"⛔ `{sembol}` için açık pozisyon yok."
        return self._bekleyeni_kur(
            "kapat", sembol, None,
            f"⚠️ *KAPATMA ONAYI*\n`{sembol}` {pos.side} · giriş `{pos.entry_price:,.6g}`\n"
            f"Piyasa emriyle KAPANACAK — geri alınamaz.")

    def _stop_iste(self, sembol: str, ham: str) -> str:
        depo, _, sembol = self._hedef_bul(sembol)
        if depo is None:
            return f"❓ `{sembol}` takip listesinde yok."
        try:
            fiyat = float(ham.replace(",", "."))
        except ValueError:
            return f"❓ Fiyat sayıya çevrilemedi: `{ham}`"
        if fiyat <= 0:
            return "⛔ Stop 0 veya negatif olamaz."
        pos = depo.get_position(sembol)
        if pos is None:
            return f"⛔ `{sembol}` için açık pozisyon yok."
        # Gevşetme mi sıkma mı — kullanıcı ne yaptığını GÖRSÜN diye yazılır.
        gevsiyor = (fiyat < pos.trailing_stop) if pos.side == "LONG" else (fiyat > pos.trailing_stop)
        yon = "GEVŞETME ⚠️ (riski artırır)" if gevsiyor else "sıkma (riski azaltır)"
        return self._bekleyeni_kur(
            "stop", sembol, fiyat,
            f"⚠️ *STOP DEĞİŞİKLİĞİ ONAYI*\n`{sembol}` {pos.side}\n"
            f"`{pos.trailing_stop:,.6g}` → `{fiyat:,.6g}` · {yon}")

    def _onayla(self, kod: str) -> str:
        b = self._bekleyen
        if not b:
            return "⛔ Bekleyen işlem yok."
        if time.time() > b["son"]:
            self._bekleyen = None
            return "⌛ Onay süresi doldu — komutu yeniden ver."
        if kod.strip() != b["kod"]:
            # Kodu yakmıyoruz ki parmak hatası işlemi iptal etmesin; süre zaten sınırlı.
            return "⛔ Kod tutmadı."
        depo, onek, sembol = self._hedef_bul(b["sembol"])
        if depo is None:
            self._bekleyen = None
            return "⛔ Sembol kayboldu — komut uygulanmadı."
        deger = "CLOSE" if b["tur"] == "kapat" else f"STOP:{b['fiyat']!r}"
        depo.set_kv(f"{onek}{sembol}", deger)
        self._bekleyen = None
        log.info("Telegram komutu kuyruğa alındı: %s%s = %s", onek, sembol, deger)
        return (f"✅ Kuyruğa alındı: `{sembol}` → `{deger}`\n"
                f"_Bot bir sonraki turunda uygular (~1 dk) ve sonucu bildirir._")

    # ------------------------------------------------------------- yönlendirme
    def _cevapla(self, metin: str) -> tuple[str, dict | None]:
        """Komutu işler ve (cevap, düğmeler) döndürür.

        Düğmeler TAM OLARAK bu çağrıda yeni bir onay beklentisi doğduysa
        eklenir. Böylece "hangi cevaba düğme konur" kararı tek yerde kalır;
        her yıkıcı komut yolu ayrı ayrı hatırlamak zorunda değil.
        """
        parcalar = metin.strip().split()
        komut = parcalar[0].split("@")[0].lower().lstrip("/") if parcalar else ""
        # Argümansız /kapat: sembol YAZDIRMAK yerine açık pozisyonları
        # düğme olarak sun. Kullanıcı sembolü ezberlemek ya da doğru yazmak
        # zorunda kalmasın — yanlış yazılan sembol zaten reddedilirdi ama
        # her seferinde bir tur kaybettiriyordu.
        if komut == "kapat" and len(parcalar) == 1:
            return self._pozisyon_secimi()

        onceki = self._bekleyen
        cevap = self._isle(metin)
        if self._bekleyen is not None and self._bekleyen is not onceki:
            return cevap, self._onay_dugmeleri(self._bekleyen["kod"])
        return cevap, None

    def _acik_pozisyonlar(self) -> list[tuple[StateStore, str]]:
        acik = [(self.state, p.symbol) for p in self.state.all_positions()]
        if self.borsa_state is not None:
            acik += [(self.borsa_state, p.symbol) for p in self.borsa_state.all_positions()]
        return acik

    def _pozisyon_secimi(self) -> tuple[str, dict | None]:
        """Açık pozisyonları tek tek düğme yapar — /kapat argümansız çağrıldığında."""
        acik = self._acik_pozisyonlar()
        if not acik:
            return "⛔ Açık pozisyon yok.", None
        # Her satırda bir sembol: dar telefon ekranında yan yana düğmeler
        # kırpılıyor ve yanlışa basma riski doğuyor.
        tuslar = [[{"text": f"❌ {sym}", "callback_data": f"kapat:{sym}"}]
                  for _, sym in acik]
        return ("Hangi pozisyonu kapatayım?\n_Seçtikten sonra ayrıca onay soracağım._",
                {"inline_keyboard": tuslar})

    def _isle(self, metin: str) -> str:
        parcalar = metin.strip().split()
        if not parcalar:
            return ""
        # "/durum@botadi" biçimi grup sohbetlerinde gelir.
        komut = parcalar[0].split("@")[0].lower().lstrip("/")
        arg = parcalar[1:]

        if komut in ("durum", "status"):
            return self._durum()
        if komut in ("yardim", "yardım", "help", "start"):
            return YARDIM
        if komut == "iptal":
            self._bekleyen = None
            return "🚫 Bekleyen işlem iptal edildi."
        if komut == "onay":
            return self._onayla(arg[0]) if arg else "❓ Kullanım: `/onay KOD`"
        if komut == "kapat":
            return self._kapat_iste(arg[0]) if arg else "❓ Kullanım: `/kapat SEMBOL`"
        if komut == "stop":
            if len(arg) < 2:
                return "❓ Kullanım: `/stop SEMBOL FİYAT`"
            return self._stop_iste(arg[0], arg[1])
        return f"❓ Bilinmeyen komut: `{komut}`\n\n{YARDIM}"

    # ------------------------------------------------------------- ana döngü
    def _ilk_offset(self) -> int:
        """Kayıtlı offset yoksa bekleyen mesajları ÇALIŞTIRMADAN atla.

        Telegram okunmamış mesajları 24 saat saklar. Bu olmadan bot her
        açılışta dünkü '/kapat'ı yeniden işlerdi.
        """
        kayitli = self.state.get_kv(OFFSET_ANAHTARI, "")
        if kayitli.isdigit():
            return int(kayitli)
        sonuc = self._cagir("getUpdates", offset=-1, timeout=0) or []
        off = (sonuc[-1]["update_id"] + 1) if sonuc else 0
        self.state.set_kv(OFFSET_ANAHTARI, str(off))
        if sonuc:
            log.info("Telegram kuyruğunda bekleyen mesajlar atlandı (offset=%d)", off)
        return off

    def _menuyu_kur(self) -> None:
        """Komutları Telegram'a kaydeder — sohbetteki "/" menüsü buradan doğar.

        Kaydedilmezse komutlar ÇALIŞIR ama görünmez; ezberden yazmak gerekir.
        Görünmeyen yetenek, olmayan yetenekle aynı kapıya çıkıyor.
        """
        # KAPSAM sahibin sohbeti. Küresel kaydedilseydi bota yazan HERKES
        # komut listesini görürdü. Yetkiyi tek kişide tutup listeyi herkese
        # göstermek tutarsız olurdu — görünmeyen kapı denenmez.
        kapsam = {"type": "chat", "chat_id": int(self.sahip)}
        if self._cagir("setMyCommands", commands=MENU, scope=kapsam) is None:
            log.warning("Telegram komut menüsü kaydedilemedi — komutlar yine de "
                        "elle yazılarak çalışır")
        else:
            log.info("Telegram komut menüsü kaydedildi (%d komut, yalnız sahibe)",
                     len(MENU))

    def _dugme(self, sorgu: dict) -> None:
        """Onay/vazgeç düğmesine basıldığında çalışır.

        YETKİ BURADA DA DENETLENİR. Düğmeli mesaj iletilebilir; iletilen
        mesajdaki düğmeye BAŞKASI basarsa callback yine bize gelir. Mesaj
        yolundaki beyaz liste bu yolu kapatmaz — ayrıca bakılmak zorunda.
        """
        kimlik = str((sorgu.get("from") or {}).get("id", ""))
        veri = sorgu.get("data") or ""
        cbid = sorgu.get("id")
        if kimlik != self.sahip:
            log.warning("Yetkisiz düğme basımı (from=%s): %r", kimlik, veri)
            self._cagir("answerCallbackQuery", callback_query_id=cbid,
                        text="Yetkiniz yok.", show_alert=True)
            return

        yeni_dugmeler = None
        if veri == "iptal":
            self._bekleyen = None
            cevap = "🚫 Vazgeçildi — hiçbir işlem yapılmadı."
        elif veri.startswith("onay:"):
            cevap = self._onayla(veri[5:])
        elif veri.startswith("kapat:"):
            # Pozisyon SEÇİLDİ, henüz kapatılmadı. Seçim tek başına yıkıcı
            # olmamalı: yanlış satıra basmak kolaydır, ikinci kapı şart.
            cevap, yeni_dugmeler = self._cevapla(f"/kapat {veri[6:]}")
        else:
            cevap = "❓ Anlaşılmayan düğme."

        # Önce düğmeyi kaldır: aynı mesaja ikinci kez basılmasın. Kod zaten
        # tükeniyor ama kullanıcıya da "bu iş bitti" demek gerekiyor.
        mesaj = sorgu.get("message") or {}
        if mesaj.get("message_id"):
            self._cagir("editMessageReplyMarkup",
                        chat_id=(mesaj.get("chat") or {}).get("id"),
                        message_id=mesaj["message_id"],
                        reply_markup={"inline_keyboard": []})
        self._cagir("answerCallbackQuery", callback_query_id=cbid)
        self._yaz(self.sahip, cevap, yeni_dugmeler)

    def calistir(self) -> None:
        if not (self.cfg.telegram_token and self.sahip):
            log.error("TELEGRAM KOMUT KATMANI KAPALI — token/chat_id eksik")
            return
        offset = self._ilk_offset()
        self._menuyu_kur()
        log.info("Telegram komut katmanı başladı (yetkili sohbet: …%s)", self.sahip[-4:])

        while True:
            try:
                guncellemeler = self._cagir("getUpdates", offset=offset,
                                            timeout=UZUN_BEKLEME_S,
                                            allowed_updates=["message", "callback_query"])
                if guncellemeler is None:
                    time.sleep(10)          # ağ/API arızası — döngü ölmesin
                    continue
                for g in guncellemeler:
                    offset = g["update_id"] + 1
                    self.state.set_kv(OFFSET_ANAHTARI, str(offset))
                    if "callback_query" in g:
                        self._dugme(g["callback_query"])
                        continue
                    mesaj = g.get("message") or {}
                    metin = (mesaj.get("text") or "").strip()
                    chat = str((mesaj.get("chat") or {}).get("id", ""))
                    if not metin or not chat:
                        continue
                    if chat != self.sahip:
                        # YETKİSİZ. Çalıştırma, sessizce de geçme — sahibe
                        # kaynak başına BİR kez haber ver (spam seli olmasın).
                        log.warning("Yetkisiz Telegram komutu (chat=%s): %r", chat, metin[:60])
                        if chat not in self._yabanci_uyarildi:
                            self._yabanci_uyarildi.add(chat)
                            self._yaz(self.sahip,
                                      f"🔒 *Yetkisiz komut denemesi*\nsohbet `{chat}` "
                                      f"şunu yazdı: `{metin[:60]}`\nÇalıştırılmadı.")
                        continue
                    log.info("Telegram komutu: %r", metin[:60])
                    cevap, dugmeler = self._cevapla(metin)
                    if cevap:
                        self._yaz(chat, cevap, dugmeler)
            except Exception as e:  # noqa: BLE001
                # Komut katmanı ÖLMEMELİ: öldüğü an kullanıcı telefondan
                # müdahale edemez ve bunu ancak ihtiyacı olduğunda fark eder.
                log.error("Telegram komut döngüsü hatası: %s", e, exc_info=True)
                time.sleep(15)
