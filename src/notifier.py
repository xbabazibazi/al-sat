"""Telegram bildirimleri.

- Token/chat id yoksa sessizce devre dışı kalır (bot çalışmaya devam eder).
- Bildirim hatası asla ana döngüyü çökertmez.
- Aynı hata mesajını art arda spamlemez (5 dk pencere).
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("notifier")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.enabled = bool(token and chat_id)
        self.token = token
        self.chat_id = chat_id
        self._last_error_msg = ""
        self._last_error_ts = 0.0

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=5,
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning("Telegram bildirimi gönderilemedi: %s", e)

    def send_error(self, message: str) -> None:
        """Hata bildirimi — aynı mesajı 5 dakika içinde tekrarlamaz."""
        now = time.time()
        if message == self._last_error_msg and now - self._last_error_ts < 300:
            return
        self._last_error_msg = message
        self._last_error_ts = now
        self.send(f"⚠️ *HATA*\n`{message[:500]}`")
