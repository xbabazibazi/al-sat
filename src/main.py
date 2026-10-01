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
from .futures_trader import (FuturesLiveTrader, FuturesPaperTrader,
                             maybe_send_futures_daily_report, snapshot_equity)
from .notifier import TelegramNotifier
from .risk import CircuitBreaker
from .state import StateStore
from .tekil import KilitTutulu, TekOrnekKilidi
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


def telegram_komut_dongusu(market) -> None:
    """Telegram komut dinleyicisi — kendi iş parçacığında, kendi bağlantısıyla.

    SQLite bağlantı nesnesi iş parçacıkları arasında paylaşılmaz; bu yüzden
    burada YENİ StateStore açılır (borsa döngüsündeki desenin aynısı).
    Pozisyona dokunmaz, komut kuyruğuna yazar — tek yazıcı hâlâ bot.
    """
    log = logging.getLogger("tgkomut")
    try:
        from .telegram_komut import TelegramKomut
        state = StateStore(CONFIG.db_path)
        borsa = StateStore(CONFIG.borsa_db_path) if CONFIG.borsa_enabled else None
        TelegramKomut(CONFIG, state, borsa, market).calistir()
    except Exception as e:  # noqa: BLE001
        log.error("Telegram komut katmanı başlatılamadı: %s", e, exc_info=True)


def _tek_ornek_ol(args, log) -> TekOrnekKilidi | None:
    """Başka bir bot çalışıyorsa BAŞLAMADAN çıkar.

    2026-09-30: sunucuda sekiz gün boyunca iki bot birden çalıştı. Eski
    koruma kalp atışının yaşına bakıyordu — yarışa açıktı ve yalnızca
    açılışta bakıyordu, bir kez ikisi de geçince bir daha hiç. Artık
    işletim sistemi kilidi: atomik ve bayatlamaz. Ayrıntı: src/tekil.py.

    KİLİT HER ŞEYDEN ÖNCE ALINIR — borsa bağlantısı, Telegram bildirimi,
    thread'ler hiç doğmadan. Fazladan örnek "Bot başlatıldı" mesajı bile
    göndermemeli; eski sürümde gönderiyordu ve gürültü kimin gerçek
    olduğunu belirsizleştiriyordu.
    """
    if args.once:
        return None        # tek turluk doğrulama koşusu kilide takılmasın
    kilit = TekOrnekKilidi(CONFIG.db_path.parent / "bot.lock")
    try:
        kilit.al()
    except KilitTutulu as e:
        # INFO, ERROR DEĞİL. Bu satır kilidin ÇALIŞTIĞININ kanıtı: nöbetçi
        # fazladan bir bot başlatmayı denedi, kilit reddetti. ERROR olarak
        # bastığımızda (2026-10-01) nöbetçi 20 saniyede bir denediği için
        # log hata seline döndü ve gerçek hatalar içinde kayboldu. Gürültü,
        # körlüğün bir başka biçimi.
        log.info("Başlatılmadı — zaten çalışan bir bot var (kilit tutuluyor). %s", e)
        sys.exit(0)   # beklenen durum; başarısızlık değil
    return kilit


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance trend-takip botu")
    parser.add_argument("--once", action="store_true", help="tek döngü çalıştır ve çık")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("main")

    # İLK İŞ. Kilit `kilit` değişkeninde TUTULUYOR: bırakılırsa (çöp
    # toplayıcı dosyayı kapatırsa) kilit de düşer, o yüzden main() boyunca
    # yaşamalı. Süreç ölünce çekirdek kendiliğinden bırakır.
    kilit = _tek_ornek_ol(args, log)  # noqa: F841 — ömrü kasıtlı olarak main() kadar

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

    futures_mode = CONFIG.mode in ("futures_paper", "futures_testnet", "futures_live")
    futures_paper_mode = CONFIG.mode == "futures_paper"

    if futures_paper_mode:
        traders = [
            FuturesPaperTrader(sym, CONFIG, market, state, notifier, breaker)
            for sym in CONFIG.symbols
        ]
        log.info("Vadeli PAPER mod | kaldıraç=%.0fx | long=%s short=%s | panel: python -m src.panel",
                 CONFIG.leverage, CONFIG.allow_long, CONFIG.allow_short)
    elif futures_mode:  # futures_testnet / futures_live
        from .futures_exchange import FuturesBroker
        futures_broker = FuturesBroker(CONFIG)
        if CONFIG.mode == "futures_live":
            log.warning("VADELİ CANLI MOD — GERÇEK PARAYLA emir gönderilecek")
        traders = [
            FuturesLiveTrader(sym, CONFIG, market, state, notifier, breaker, futures_broker)
            for sym in CONFIG.symbols
        ]
        log.info("Vadeli %s mod | kaldıraç=%.0fx | long=%s short=%s",
                 CONFIG.mode, CONFIG.leverage, CONFIG.allow_long, CONFIG.allow_short)
        # Açılış mutabakatı: borsa gerçeği DB'yi ezer (bkz. docs/07-Canliya-Gecis.md)
        for t in traders:
            try:
                t.reconcile()
            except Exception as e:
                log.error("[%s] Mutabakat hatası: %s", t.symbol, e)
                notifier.send_error(f"{t.symbol} mutabakat hatası: {e}")
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

    # Panelin "çalışıyor mu" bilmesi için kalp atışı + süreç kimliği.
    # NOT: çifte çalışma koruması ARTIK BURADA DEĞİL — main()'in en başında,
    # işletim sistemi kilidiyle yapılıyor (aşağıdaki _tek_ornek_ol).
    state.set_kv("bot_pid", str(os.getpid()))

    # BORSA kanalı (daemon: ana süreç ölünce o da ölür; --once turunda açılmaz)
    if CONFIG.borsa_enabled and not args.once:
        threading.Thread(target=borsa_dongusu, args=(notifier,),
                         name="borsa", daemon=True).start()

    # TELEGRAM KOMUT KATMANI — telefondan /durum, /kapat, /stop.
    # DAEMON OLMASI KASITLI: bot ölürse komut katmanı da ölsün. Aksi hâlde
    # "komut kabul edildi" cevabı gelir ama uygulayacak bot yoktur — bu
    # projedeki en tehlikeli arıza tipi (başarısızlık başarı gibi görünür).
    if CONFIG.telegram_komut and not args.once:
        threading.Thread(target=telegram_komut_dongusu, args=(market,),
                         name="tgkomut", daemon=True).start()

    while True:
        # KALP ATIŞI HER SEMBOLDE ATAR, tur başında BİR KEZ değil.
        # 2026-10-01: sunucunun DNS'i düştü, her poll 10 sn timeout'a girdi
        # ve 10 paritelik tur 100 saniyeyi aştı. Panelin tazelik penceresi
        # 90 sn olduğu için SAĞLIKLI bot "düşmüş" sayıldı; nöbetçi 20
        # saniyede bir yenisini başlattı, her biri kilide çarpıp ERROR
        # bastı. Yani kalp atışı "yaşıyorum" değil "turu bitirdim" diyordu —
        # ikisi aynı şey değil ve fark tam da arıza anında açılıyor.
        for t in traders:
            state.set_kv("bot_heartbeat", datetime.now(timezone.utc).isoformat())
            try:
                t.poll()
            except Exception as e:
                log.error("[%s] Döngü hatası: %s", t.symbol, e, exc_info=True)
                notifier.send_error(f"{t.symbol} döngü hatası: {e}")
            time.sleep(1)  # semboller arası kısa es — rate limit nezaketi
        state.set_kv("bot_heartbeat", datetime.now(timezone.utc).isoformat())

        try:
            if futures_mode:
                snapshot_equity(traders, state)  # panel varlık grafiği için
                if futures_paper_mode:
                    # $10.000 sanal başlangıç varsayar — canlı/testnet'te bakiye
                    # keyfi olduğu için burada YANLIŞ yüzde üretirdi. Canlı
                    # günlük özeti henüz yazılmadı (bilinçli eksik, bkz. docs/07).
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
