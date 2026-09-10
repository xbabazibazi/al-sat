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
        if not self.enabled:
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=8,
            )
            if resp.status_code >= 400:
                # Gövdeyi oku: Telegram sebebi burada yazar ("chat not found",
                # "Unauthorized", "can't parse entities"). Sebepsiz hata,
                # teşhis edilemeyen hatadır.
                try:
                    sebep = resp.json().get("description", resp.text[:200])
                except Exception:  # noqa: BLE001
                    sebep = resp.text[:200]
                self._hata += 1
                log.error("Telegram gönderilemedi (HTTP %s): %s", resp.status_code, sebep)
                self._saglik(False, f"HTTP {resp.status_code}: {sebep}")
                return False
            self._basari += 1
            self._saglik(True, "")
            return True
        except Exception as e:  # noqa: BLE001
            self._hata += 1
            log.error("Telegram bildirimi gönderilemedi: %s", e)
            self._saglik(False, str(e)[:200])
            return False

    def send_error(self, message: str) -> bool:
        """Hata bildirimi — aynı mesajı 5 dakika içinde tekrarlamaz."""
        now = time.time()
        if message == self._last_error_msg and now - self._last_error_ts < 300:
            return False
        self._last_error_msg = message
        self._last_error_ts = now
        return self.send(f"⚠️ *HATA*\n`{message[:500]}`")
