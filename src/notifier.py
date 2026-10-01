"""Telegram bildirimleri.

- Bildirim hatası asla ana döngüyü çökertmez.
- Aynı hata mesajını art arda spamlemez (5 dk pencere).

SESSİZ ÖLÜM SORUNU (2026-09-10'da canlıda yaşandı):
  Kullanıcı Telegram token'ını yeniledi; bot OPUSDT SHORT açtı ve HİÇBİR
  bildirim gitmedi. İki ayrı sessizlik mekanizması vardı:
    1) token/chat_id boşsa `enabled=False` olup send() hiç denemeden dönüyordu
       — hiçbir yerde hiçbir iz kalmıyordu.
    2) token geçersizse istek 401 dönüyor, `except` yutuyor ve yalnızca
       data/bot.log'a warning düşüyordu; oraya kimse bakmıyor.
  Sonuç: "bildirim gelmedi" ile "işlem olmadı" birbirinden AYIRT EDİLEMİYORDU.

  Bu modülün özel bir yanı var: arızasını KENDİ KANALINDAN duyuramaz.
  O yüzden ikinci bir kanal şart — sağlık durumu `saglik_yaz` geri çağrımıyla
  kalıcı duruma yazılır, panel bunu okuyup büyük bir uyarı gösterir.
  Aynı aile: cron sessizliği (kalp atışı damgası), kilit sızıntısı
  (kilit_serbest), komut yutulması (cmdres) — hepsinde çözüm aynıydı:
  BAŞARISIZLIK, BAŞARIYLA AYNI GÖRÜNMEMELİ.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("notifier")

# Panelin okuduğu anahtar. Değerin biçimi: JSON (bkz. _saglik).
SAGLIK_ANAHTARI = "telegram_saglik"

# GEÇİCİ ARIZADA YENİDEN DENE. 2026-10-01 01:55-01:59 arasında sunucunun
# DNS'i düştü (NameResolutionError) ve Telegram'a da Binance'e de
# ulaşılamadı. O pencerede gönderilen bildirimler KALICI OLARAK KAYBOLDU —
# tek deneme yapılıyordu. Pozisyon kapanış bildirimi kaybolursa kullanıcı
# işlemi hiç duymaz; ağ birkaç saniyelik hıçkırık yüzünden bu kabul edilemez.
DENEME_SAYISI = 3
DENEME_BEKLEME_S = (2, 5)        # 1. ve 2. başarısızlıktan sonraki bekleme

# Kalıcı hatalar: yeniden denemek anlamsız, sadece gecikme üretir.
# 401 (geçersiz token), 400 (bozuk markdown), 403 (bot engellendi),
# 404 (chat yok) — bunlar tekrar denemekle düzelmez.
# 429 (hız sınırı) ve 5xx (Telegram tarafı) GEÇİCİDİR, denenir.
KALICI_HTTP = (400, 401, 403, 404)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, saglik_yaz=None):
        """saglik_yaz: callable(anahtar, deger) — genelde StateStore.set_kv.
        Verilmezse sağlık takibi yapılmaz (dağıtım betiğindeki tek atışlık
        kullanım gibi yerlerde gereksiz)."""
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._saglik_yaz = saglik_yaz
        self._last_error_msg = ""
        self._last_error_ts = 0.0
        self._basari = 0
        self._hata = 0

        if not self.enabled:
            # SESSİZ KALMA. Eksik ayar, en sık karşılaşılan ve en sinsi hâl.
            eksik = []
            if not token:
                eksik.append("TELEGRAM_BOT_TOKEN")
            if not chat_id:
                eksik.append("TELEGRAM_CHAT_ID")
            log.error("TELEGRAM KAPALI — .env'de eksik: %s. Hiçbir bildirim "
                      "gitmeyecek (işlem açılsa bile).", ", ".join(eksik))
            self._saglik(False, f".env'de eksik: {', '.join(eksik)}")

    # ------------------------------------------------------------------ sağlık
    def _saglik(self, ok: bool, sebep: str = "") -> None:
        """Bildirim kanalının durumunu panelin görebileceği yere yazar."""
        if self._saglik_yaz is None:
            return
        try:
            self._saglik_yaz(SAGLIK_ANAHTARI, json.dumps({
                "ok": ok,
                "sebep": sebep,
                "ts": datetime.now(timezone.utc).isoformat(),
                "basari": self._basari,
                "hata": self._hata,
                "yapilandirildi": self.enabled,
            }, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram sağlık durumu yazılamadı: %s", e)

    def dogrula(self) -> bool:
        """Açılışta token'ı SINA (getMe). İlk işlemi beklemeden arızayı bildirir.

        Neden açılışta: bir işlem günlerce açılmayabilir. Token bozuksa bunu
        ilk işlem anında değil, bot başlar başlamaz bilmek gerekir.
        """
        if not self.enabled:
            return False
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getMe",
                             timeout=8)
            if r.status_code == 401:
                self._hata += 1
                log.error("TELEGRAM TOKEN GEÇERSİZ (401) — token yenilendiyse "
                          ".env güncellenmemiş olabilir. Bildirim GİTMEYECEK.")
                self._saglik(False, "token geçersiz (401) — .env'deki "
                                    "TELEGRAM_BOT_TOKEN eski olabilir")
                return False
            r.raise_for_status()
            ad = (r.json().get("result") or {}).get("username", "?")
            log.info("Telegram doğrulandı: @%s", ad)
            self._saglik(True, f"@{ad} doğrulandı")
            return True
        except Exception as e:  # noqa: BLE001
            self._hata += 1
            log.error("Telegram doğrulanamadı: %s", e)
            self._saglik(False, f"doğrulama başarısız: {e}")
            return False

    # ----------------------------------------------------------------- gönderim
    def send(self, message: str) -> bool:
        """Bildirimi gönderir. Geçici arızada yeniden dener, kalıcıda denemez.

        Ayrımın önemi: ağ hıçkırığında (DNS, timeout, 5xx) tekrar denemek
        bildirimi KURTARIR; geçersiz token ya da bozuk Markdown'da tekrar
        denemek yalnızca gecikme üretir ve ana döngüyü bekletir.
        """
        if not self.enabled:
            return False
        son_sebep = "bilinmiyor"
        for deneme in range(DENEME_SAYISI):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": message,
                          "parse_mode": "Markdown"},
                    timeout=8,
                )
            except Exception as e:  # noqa: BLE001 — ağ katmanı: GEÇİCİ say
                son_sebep = str(e)[:200]
                if self._bekle(deneme, son_sebep):
                    continue
                break
            if resp.status_code >= 400:
                # Gövdeyi oku: Telegram sebebi burada yazar ("chat not found",
                # "Unauthorized", "can't parse entities"). Sebepsiz hata,
                # teşhis edilemeyen hatadır.
                try:
                    sebep = resp.json().get("description", resp.text[:200])
                except Exception:  # noqa: BLE001
                    sebep = resp.text[:200]
                son_sebep = f"HTTP {resp.status_code}: {sebep}"
                if resp.status_code in KALICI_HTTP:
                    self._hata += 1
                    log.error("Telegram gönderilemedi (KALICI, HTTP %s): %s",
                              resp.status_code, sebep)
                    self._saglik(False, son_sebep)
                    return False
                if self._bekle(deneme, son_sebep):
                    continue
                break
            self._basari += 1
            self._saglik(True, "")
            if deneme:
                log.info("Telegram bildirimi %d. denemede gitti", deneme + 1)
            return True

        self._hata += 1
        log.error("Telegram bildirimi %d denemede de gönderilemedi: %s",
                  DENEME_SAYISI, son_sebep)
        self._saglik(False, son_sebep)
        return False

    def _bekle(self, deneme: int, sebep: str) -> bool:
        """Yeniden deneme hakkı varsa bekler ve True döner."""
        if deneme >= DENEME_SAYISI - 1:
            return False
        sure = DENEME_BEKLEME_S[min(deneme, len(DENEME_BEKLEME_S) - 1)]
        log.warning("Telegram geçici arıza (%d/%d), %d sn sonra tekrar: %s",
                    deneme + 1, DENEME_SAYISI, sure, sebep)
        time.sleep(sure)
        return True

    def send_error(self, message: str) -> bool:
        """Hata bildirimi — aynı mesajı 5 dakika içinde tekrarlamaz."""
        now = time.time()
        if message == self._last_error_msg and now - self._last_error_ts < 300:
            return False
        self._last_error_msg = message
        self._last_error_ts = now
        return self.send(f"⚠️ *HATA*\n`{message[:500]}`")


def saglik_canli_yaz(set_kv, ok: bool, sebep: str = "") -> None:
    """Komut katmanının getUpdates sonucunu sağlık kaydına işler.

    NEDEN GEREKLİ (2026-10-01): kayıt yalnızca bir bildirim GÖNDERİLDİĞİNDE
    güncelleniyordu. Bot sakin geçen saatlerde hiç göndermiyor, dolayısıyla
    kayıt donuyor: 01:58'deki geçici DNS arızası saat 04:40'ta hâlâ "Telegram
    BOZUK" diye duruyordu. Oysa komut katmanı ~50 saniyede bir getUpdates
    çağırıyor — kanalın canlı olup olmadığını zaten BİLİYORUZ, sadece
    yazmıyorduk. Elde olan sinyali kullanmamak, körlüğü kendi elimizle
    sürdürmek demekti.

    Panel bu kaydın YAŞINA da bakar; taze bir "ok" ile üç saatlik bir
    "bozuk" aynı şey değildir.
    """
    try:
        set_kv(SAGLIK_ANAHTARI, json.dumps({
            "ok": ok,
            "sebep": sebep,
            "ts": datetime.now(timezone.utc).isoformat(),
            "kaynak": "komut-katmani",   # gönderim değil, canlılık yoklaması
            "yapilandirildi": True,
        }, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram canlılık durumu yazılamadı: %s", e)
