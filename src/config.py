"""Merkezi yapılandırma: .env + varsayılanlar.

Tüm ayarlar tek yerden okunur; strateji, canlı bot ve backtest aynı
değerleri kullanır ki backtest ile canlı davranış birbirinden sapmasın.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class StrategyParams:
    """Strateji parametreleri — backtest ve canlı bot İKİSİ DE bunu kullanır."""
    ema_period: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    donchian_period: int = int(_env("DONCHIAN_PERIOD", "20"))  # kırılım kanalı (10=aktif, 20=sabırlı)
    atr_multiplier: float = float(_env("ATR_MULTIPLIER", "3.0"))  # 2022-2026 taramasında sağlam bölge
    rsi_max_entry: float = 80.0        # aşırı alımda (blow-off) girişleri engelle
    warmup_bars: int = 220             # EMA200'ün oturması için gereken minimum bar
    # Günlük trend teyidi: yalnızca GÜNLÜK kapanış da günlük EMA'nın üzerindeyse gir.
    # A/B testi (2022-2026): SOL'da belirgin fayda, BTC/ETH'de belirgin ZARAR →
    # tutarsız olduğu için varsayılan KAPALI. Denemek isteyen .env'den açabilir.
    use_daily_filter: bool = _env("USE_DAILY_FILTER", "false").lower() == "true"
    daily_ema_period: int = 200


@dataclass(frozen=True)
class Config:
    mode: str = _env("BOT_MODE", "dry_run").lower()  # dry_run | testnet | live | futures_paper

    # ---- Vadeli işlem (futures_paper) ayarları ----
    # Kaldıraç neden 2? (2026-09-07 ölçümü — PORTFÖY motoru, backtest/portfoy.py)
    #
    # ÖNCEKİ KARAR GEÇERSİZ ÇIKTI. 2026-09-06'da ayar_etkisi.py ile 1x ve 2x
    # BİREBİR ÖZDEŞ ölçülmüş ve "2x bedelsiz risk" diye 1x'e inilmişti. O test
    # AYRI BAKİYELİYDİ (her parite kendi $10.000'i) — orada tek pozisyon marj
    # tavanına hiç değmez, dolayısıyla kaldıraç ölü değişkendi. Canlı bot ise
    # TEK bakiyeyi paylaşıyor ve aynı anda 4 pozisyon tutuyor; orada marj
    # gerçekten bağlayıcı. Ortak bakiyeli portföy motorunda yeniden ölçüldü:
    #
    #        Getiri   MaxDD   Sharpe  İşlem  Kazanma  Likid.
    #   1x   %94.7   -%27.8    0.86    1356   %39.1     0
    #   2x  %128.7   -%29.6    0.93    1356   %39.1     0     <- seçildi
    #   3x  %144.6   -%31.2    0.96    1356   %39.1     0
    #   5x  %149.4   -%33.3    0.95    1356   %39.1     8     <- duvar
    #
    # MEKANİZMA: işlem sayısı ve kazanma oranı değişmiyor (1356 / %39.1) —
    # aynı işlemler, farklı boyut. 1x'te açık pozisyon nakdin tamamını kilitler,
    # 2./3./4. pozisyon kalan cılız nakitten boyutlanır. $10k equity + 3 açık
    # pozisyon örneğinde 1x nakdi $5.500'e düşürür (4. işlem equity'nin %0.55'i
    # kadar risk alır), 2x'te marj yarıya inip nakit $7.750 olur (%0.78). Yani
    # 1x "daha az risk" değil, NİYET EDİLEN %1'in altında kalmak.
    # Üç kapı da geçildi: 1. yarı %5.8->%12.4, 2. yarı %93.0->%114.0, tam dönem.
    #
    # NEDEN 3x/5x DEĞİL: likidasyon tamponu = %90/kaldıraç (LIQ_BUFFER).
    # 1x->%90, 2x->%45, 3x->%30, 5x->%18. 3x'in %30 tamponu 1356 işlemde hiç
    # delinmedi; 2x onun 1.5 katı pay bırakıyor. 5x'te 8 likidasyon = sınır.
    # Kâğıt aşamasında marj bırakmayı tercih ediyoruz; canlı veriyle (Faz 1
    # kapısı, ~100 kapanmış işlem) 3x yeniden değerlendirilebilir.
    #
    # AÇIK POZİSYONLARA ETKİSİ YOK: leverage yalnızca _open() içinde okunur;
    # açık pozisyonlar kendi `margin` değerlerini kayıtta taşır.
    #
    # SHORT: eski 3-parite testinde 9/9 zarar etmişti, ama 10 paritelik yeni
    # ölçümde short tarafı KÂRLI ve getirinin ana kaynağı (sadece-long %4.8'e
    # karşı long+short %11.8). .env'de ALLOW_SHORT=true ile açık.
    leverage: float = float(_env("LEVERAGE", "1"))
    allow_long: bool = _env("ALLOW_LONG", "true").lower() == "true"
    allow_short: bool = _env("ALLOW_SHORT", "false").lower() == "true"
    futures_taker_fee: float = 0.0005          # USDT-M taker %0.05
    funding_daily_long: float = 0.0003         # long öder: ~%0.01/8s
    funding_daily_short: float = 0.00015       # muhafazakâr short varsayımı
    panel_port: int = int(_env("PANEL_PORT", "8484"))
    # Sunucuda çalışırken 0.0.0.0 gerekir (container dışından erişim için).
    # Yayınlanan portu MUTLAKA Tailscale IP'sine bağlayın — bkz. docker-compose.
    panel_host: str = _env("PANEL_HOST", "127.0.0.1")
    # Panelden güncelleme tetikleme ucu (/api/guncelle). Panel yalnızca Tailscale
    # arayüzünde dinlediği için ağ dışına kapalı, tetik de SABİT bir betiği
    # çalıştırır (parametre almaz). Yine de ağdaki herkese "dağıtımı başlat"
    # yetkisi verir; kapatmak için .env'e DEPLOY_ENDPOINT=false yaz.
    # Teşhis ucu (/api/deploy-durum) salt-okunurdur, bu bayrakla kapanmaz.
    deploy_endpoint: bool = _env("DEPLOY_ENDPOINT", "true").lower() == "true"
    symbols: tuple[str, ...] = tuple(
        s.strip().upper() for s in _env("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()
    )
    timeframe: str = _env("TIMEFRAME", "4h")  # 1h backtestte komisyona yenildi; 4h sağlam

    # Risk yönetimi
    risk_pct: float = float(_env("RISK_PCT", "0.02"))                 # işlem başına risk: bakiyenin %2'si
    max_daily_loss_pct: float = float(_env("MAX_DAILY_LOSS_PCT", "0.05"))  # günlük devre kesici: %5
    max_balance_usage: float = 0.95    # bakiyenin en fazla %95'i tek pozisyona girebilir

    # PORTFÖY KORUMASI — eşzamanlı pozisyon tavanı (0 = sınırsız)
    # Neden: 10 paritenin ortalama ikili korelasyonu 0.681 ölçüldü. Bu yüzden
    # "10 ayrı pozisyonda %1 risk" aslında BAĞIMSIZ 10 bahis değil; formül
    #   gerçek risk = r×√(N + N(N−1)ρ) = %1×√(10+90×0.681) ≈ %8.4
    #   bağımsız bahis = N/(1+(N−1)ρ) = 1.4
    # yani tek yönde ~%8.4'lük TEK bir bahis. Portföy backtest'i (tek bakiye,
    # backtest/portfoy.py) bunu doğruladı: tavansız MaxDD %31.5 — oysa ayrı
    # bakiyeli eski ölçümler %12 gösteriyordu.
    # Ölçüm: tavan 4 ile MaxDD %31.5→%27.8, Sharpe 0.74→0.86, getiri %99.9→%94.7.
    # Bu bir GETİRİ optimizasyonu değil, kuyruk riski kontrolüdür.
    # NOT: YÖN tavanı (aynı yönde en fazla N) ayrıca test edildi ve ZARARLI
    # çıktı (Sharpe 0.86→0.57) — stratejinin kârı baskın yönde olmaktan geliyor.
    # Sayıyı sınırla, yönü değil.
    max_concurrent_positions: int = int(_env("MAX_CONCURRENT_POSITIONS", "4"))

    # KÂR BİLDİRİMİ — pozisyon N×R kâra ulaşınca Telegram'a haber ver (0 = kapalı).
    # KAPATMAZ. Otomatik kâr hedefi test edildi ve mevcut parite listesiyle
    # sağlamlık çıtasını geçemedi (ilk yarıda berabere), o yüzden karar
    # kullanıcıda: bildirim gelir, dilerse panelden KAPAT'a basar.
    # Zaten 3×ATR iz süren stop ile 4R'ye ulaşıldığında stop matematiksel olarak
    # giriş+3R'de kilitlidir (tepe−3ATR = giriş+12ATR−3ATR), yani kârın dörtte
    # üçü garanti altındadır — "stop'u yukarı çek" ihtiyacı otomatik karşılanır.
    # Varsayılan 4.0'dı; kullanıcı HER R'da haber istedi (2026-09-10) → 1.0.
    # Her tam R eşiği (1R, 2R, 3R...) BİR kez bildirilir; eşik etrafında
    # gidip gelme spam yapmaz (r_notified cırcırı yalnızca yukarı sayar).
    r_notify_level: float = float(_env("R_NOTIFY_LEVEL", "1.0"))

    # Komisyon optimizasyonu:
    # - Binance'te "BNB ile komisyon öde" açıksa spot ücret %0.10 → %0.075 düşer.
    #   (Bunu borsa arayüzünden açmanız ve az miktar BNB tutmanız gerekir.)
    # - use_limit_entry: girişte önce maker limit emri dener (taker yerine maker ücreti);
    #   dolmazsa timeout sonunda iptal edip market emrine döner.
    use_limit_entry: bool = _env("USE_LIMIT_ENTRY", "true").lower() == "true"
    limit_entry_timeout_s: int = int(_env("LIMIT_ENTRY_TIMEOUT_S", "45"))

    # API anahtarları
    testnet_key: str = _env("BINANCE_TESTNET_KEY")
    testnet_secret: str = _env("BINANCE_TESTNET_SECRET")
    live_key: str = _env("BINANCE_LIVE_KEY")
    live_secret: str = _env("BINANCE_LIVE_SECRET")

    # Telegram
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID")

    # Zamanlama
    poll_seconds: int = 30             # ana döngü periyodu (stop takibi + mum kontrolü)
    daily_report_hour: int = int(_env("DAILY_REPORT_HOUR", "21"))

    # ---- BORSA kanalı (ABD + BIST, SANAL cüzdanla paper) ----
    # Kripto botundan tamamen ayrı: ayrı DB, ayrı cüzdanlar (USD + TRY), ayrı
    # iş parçacığı. Günlük mum + haftalık teyit; BIST long-only (borsa_trader.py).
    # Kapatmak için .env'e BORSA_ENABLED=false yaz.
    borsa_enabled: bool = _env("BORSA_ENABLED", "true").lower() == "true"
    borsa_symbols: tuple[str, ...] = tuple(
        s.strip().upper() for s in _env(
            "BORSA_SYMBOLS",
            "SPY,QQQ,NVDA,AAPL,MSFT,AMZN,META,GOOGL,TSLA,AMD,"
            "XU100.IS,THYAO.IS,ASELS.IS,GARAN.IS,AKBNK.IS,EREGL.IS,TUPRS.IS,"
            "SISE.IS,KCHOL.IS,BIMAS.IS").split(",") if s.strip()
    )
    borsa_poll_seconds: int = int(_env("BORSA_POLL_SECONDS", "300"))  # veri 15dk gecikmeli; sık sormak anlamsız
    borsa_risk_pct: float = float(_env("BORSA_RISK_PCT", "0.01"))     # işlem başına cüzdanın %1'i
    borsa_max_positions: int = int(_env("BORSA_MAX_POSITIONS", "3"))  # cüzdan başına eşzamanlı tavan
    borsa_db_path: Path = DATA_DIR / "borsa_state.db"

    # Dosyalar
    db_path: Path = DATA_DIR / "bot_state.db"
    log_path: Path = DATA_DIR / "bot.log"

    strategy: StrategyParams = field(default_factory=StrategyParams)

    def validate(self) -> None:
        if self.mode not in ("dry_run", "testnet", "live", "futures_paper"):
            raise ValueError(f"Geçersiz BOT_MODE: {self.mode}")
        if self.mode == "futures_paper" and not (1 <= self.leverage <= 5):
            raise ValueError("LEVERAGE 1-5 arasında olmalı — üstü backtest'te değer üretmedi, risk üretti")
        if self.mode == "testnet" and not (self.testnet_key and self.testnet_secret):
            raise ValueError("testnet modu için BINANCE_TESTNET_KEY/SECRET gerekli (testnet.binance.vision)")
        if self.mode == "live" and not (self.live_key and self.live_secret):
            raise ValueError("live modu için BINANCE_LIVE_KEY/SECRET gerekli")
        if not (0 < self.risk_pct <= 0.05):
            raise ValueError("RISK_PCT 0 ile 0.05 (%5) arasında olmalı — daha yükseği kumardır")


CONFIG = Config()
