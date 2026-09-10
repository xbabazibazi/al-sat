"""Bot giriş noktası.

Kullanım:
    python -m src.main            # sürekli çalışır
    python -m src.main --once     # tek tur (kurulum doğrulama/test için)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .config import CONFIG
from .exchange import MarketData, build_broker
from .futures_trader import (FuturesPaperTrader, maybe_send_futures_daily_report,
                             snapshot_equity)
from .notifier import TelegramNotifier
from .risk import CircuitBreaker
from .state import StateStore
from .trader import SymbolTrader, maybe_send_daily_report


def setup_logging() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1254 konsolu için
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        CONFIG.log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def borsa_dongusu(notifier: TelegramNotifier) -> None:
    """BORSA kanalı — kendi iş parçacığında, kendi DB'siyle, kendi hızında.

    Kripto döngüsünden TAM YALITIM: burada ne olursa olsun (yfinance çöker,
    ağ gider, Yahoo bizi engeller) kripto botu etkilenmez. Bu yüzden ayrı
    thread + ayrı StateStore + geniş try/except. Tersi de doğru: bu döngü
    kripto DB'sine hiç dokunmaz (tek yazıcı ilkesi iki dünyada da geçerli).
    """
    log = logging.getLogger("borsa")
    try:
        from .borsa_data import BorsaMarket
        from .borsa_trader import BorsaPaperTrader
    except ImportError as e:
        log.error("BORSA kanalı başlatılamadı (yfinance kurulu mu?): %s", e)
        notifier.send_error(f"BORSA kanalı devre dışı — modül eksik: {e}")
        return

    state = StateStore(CONFIG.borsa_db_path)
    market = BorsaMarket(CONFIG.borsa_symbols)
    traders = [BorsaPaperTrader(s, CONFIG, market, state, notifier)
               for s in CONFIG.borsa_symbols]
    log.info("BORSA kanalı başladı | %d sembol | döngü %ds | SANAL cüzdan",
             len(traders), CONFIG.borsa_poll_seconds)

    while True:
        state.set_kv("borsa_heartbeat", datetime.now(timezone.utc).isoformat())
        for t in traders:
            try:
                t.poll()
            except Exception as e:
                log.error("[%s] Borsa döngü hatası: %s", t.symbol, e, exc_info=True)
        time.sleep(CONFIG.borsa_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance trend-takip botu")
    parser.add_argument("--once", action="store_true", help="tek döngü çalıştır ve çık")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("main")

    CONFIG.validate()
    log.info("Bot başlıyor | mod=%s | semboller=%s | zaman dilimi=%s | risk=%%%.1f | ATR×%.1f",
             CONFIG.mode, ",".join(CONFIG.symbols), CONFIG.timeframe,
             CONFIG.risk_pct * 100, CONFIG.strategy.atr_multiplier)

    market = MarketData()
    state = StateStore(CONFIG.db_path)
    # saglik_yaz: bildirim kanalının durumu panele AYRI bir kanaldan ulaşsın.
    # Telegram bozulduğunda bunu Telegram'dan duyuramayız (2026-09-10 dersi).
    notifier = TelegramNotifier(CONFIG.telegram_token, CONFIG.telegram_chat_id,
                                saglik_yaz=state.set_kv)
    notifier.dogrula()   # token'ı ilk işlemi beklemeden sına
    breaker = CircuitBreaker(state, CONFIG.max_daily_loss_pct)

    futures_mode = CONFIG.mode == "futures_paper"
    if futures_mode:
        traders = [
            FuturesPaperTrader(sym, CONFIG, market, state, notifier, breaker)
            for sym in CONFIG.symbols
        ]
        log.info("Vadeli PAPER mod | kaldıraç=%.0fx | long=%s short=%s | panel: python -m src.panel",
                 CONFIG.leverage, CONFIG.allow_long, CONFIG.allow_short)
    else:
        broker = build_broker(CONFIG, market, state)
        traders = [
            SymbolTrader(sym, CONFIG, market, broker, state, notifier, breaker)
            for sym in CONFIG.symbols
        ]
        # Açılış mutabakatı: kayıtlı durum ile borsa gerçeğini eşitle
        for t in traders:
            try:
                t.reconcile()
            except Exception as e:
                log.error("[%s] Mutabakat hatası: %s", t.symbol, e)
                notifier.send_error(f"{t.symbol} mutabakat hatası: {e}")

    notifier.send(
        f"🤖 *Bot başlatıldı* | mod: `{CONFIG.mode}` | "
        f"semboller: `{', '.join(CONFIG.symbols)}` | tf: `{CONFIG.timeframe}`"
    )

    # ÇİFTE ÇALIŞMA KİLİDİ: başka bir bot canlıysa (kalp atışı taze) çık.
    # İki bot aynı DB'ye yazarsa çifte pozisyon açılabilir.
    hb = state.get_kv("bot_heartbeat")
    other_pid = state.get_kv("bot_pid")
    if hb and other_pid != str(os.getpid()):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds()
        except ValueError:
            age = 1e9
        if age < 120 and not args.once:
            log.error("Zaten çalışan bir bot var (pid=%s, kalp atışı %.0f sn önce). "
                      "Çifte işlem riski nedeniyle çıkılıyor.", other_pid, age)
            sys.exit(1)

    # Panelin "çalışıyor mu" bilmesi için kalp atışı + süreç kimliği
    state.set_kv("bot_pid", str(os.getpid()))

    # BORSA kanalı (daemon: ana süreç ölünce o da ölür; --once turunda açılmaz)
    if CONFIG.borsa_enabled and not args.once:
        threading.Thread(target=borsa_dongusu, args=(notifier,),
                         name="borsa", daemon=True).start()

    while True:
        state.set_kv("bot_heartbeat", datetime.now(timezone.utc).isoformat())
        for t in traders:
            try:
                t.poll()
            except Exception as e:
                log.error("[%s] Döngü hatası: %s", t.symbol, e, exc_info=True)
                notifier.send_error(f"{t.symbol} döngü hatası: {e}")
            time.sleep(1)  # semboller arası kısa es — rate limit nezaketi

        try:
            if futures_mode:
                snapshot_equity(traders, state)  # panel varlık grafiği için
                maybe_send_futures_daily_report(CONFIG, traders, state, notifier)
            else:
                maybe_send_daily_report(CONFIG, market, broker, state, notifier)
        except Exception as e:
            log.error("Dönem sonu görev hatası: %s", e)

        if args.once:
            log.info("--once: tek tur tamamlandı, çıkılıyor.")
            break
        time.sleep(CONFIG.poll_seconds)


if __name__ == "__main__":
    main()
