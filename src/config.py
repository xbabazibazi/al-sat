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
    # Kaldıraç neden 1? (2026-09-06 ölçümü — backtest/ayar_etkisi.py)
    # 10 paritede 1x ve 2x satırları BİREBİR ÖZDEŞ çıktı: %10.8 getiri,
    # Sharpe 0.46, 31.4 işlem/yıl. Sebep: pozisyon boyutunu risk kuralı
    # (RISK_PCT / stop mesafesi) belirler; kaldıraç yalnızca bakiye tavanını
    # gevşetir ve o tavana hiç değinilmez. Yani 2x, karşılığında hiçbir getiri
    # vermeden likidasyon riski taşıyordu -> 1x'e indirildi (bedelsiz güvenlik).
    # RISK_PCT belirgin artırılırsa tavan bağlayıcı olabilir; o zaman gözden geçir.
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
    symbols: tuple[str, ...] = tuple(
        s.strip().upper() for s in _env("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()
    )
    timeframe: str = _env("TIMEFRAME", "4h")  # 1h backtestte komisyona yenildi; 4h sağlam

    # Risk yönetimi
    risk_pct: float = float(_env("RISK_PCT", "0.02"))                 # işlem başına risk: bakiyenin %2'si
    max_daily_loss_pct: float = float(_env("MAX_DAILY_LOSS_PCT", "0.05"))  # günlük devre kesici: %5
    max_balance_usage: float = 0.95    # bakiyenin en fazla %95'i tek pozisyona girebilir

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
