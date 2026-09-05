"""Canlı işlem motoru.

Sohbetteki örnek kodların aksine buradaki tasarım şu garantileri verir:

- MUM BAŞINA TEK KARAR: işlenen son mumun open_time'ı SQLite'ta tutulur;
  aynı mum ikinci kez değerlendirilmez (satış sonrası anında geri alım yok).
- BORSA TARAFINDA STOP: alımdan hemen sonra STOP_LOSS_LIMIT emri borsaya
  konur. İzleyen stop yükseldikçe emir iptal edilip yenisi konur.
- KALICI DURUM: pozisyon/stop bilgisi diskte; yeniden başlatmada borsayla
  mutabakat (reconciliation) yapılır.
- GERÇEK VERİ: sinyaller her modda prod verisiyle üretilir.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from .config import Config
from .exchange import Broker, BinanceBroker, MarketData, SymbolFilters, floor_to_step
from .notifier import TelegramNotifier
from .risk import CircuitBreaker, position_size
from .state import Position, StateStore
from .strategy import check_entry, compute_indicators, initial_stop, updated_trailing_stop

log = logging.getLogger("trader")

STOP_LIMIT_OFFSET = 0.003  # stop limit fiyatı, stop tetiğinin %0.3 altı (dolum garantisi)


class SymbolTrader:
    def __init__(self, symbol: str, cfg: Config, market: MarketData, broker: Broker,
                 state: StateStore, notifier: TelegramNotifier, breaker: CircuitBreaker):
        self.symbol = symbol
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.state = state
        self.notifier = notifier
        self.breaker = breaker
        self.filters: SymbolFilters = self._venue_filters()

    def _venue_filters(self) -> SymbolFilters:
        # Emirler hangi borsaya gidiyorsa filtreler oradan; dry_run'da prod filtreleri
        if isinstance(self.broker, BinanceBroker):
            return self.broker.venue_filters(self.symbol)
        return self.market.filters(self.symbol)

    # ------------------------------------------------------------------ yardımcılar
    def _fmt_stop_prices(self, stop: float) -> tuple[float, float]:
        stop_p = floor_to_step(stop, self.filters.tick_size)
        limit_p = floor_to_step(stop * (1 - STOP_LIMIT_OFFSET), self.filters.tick_size)
        return stop_p, limit_p

    def _sellable_qty(self) -> float:
        return floor_to_step(self.broker.base_balance(self.symbol), self.filters.step_size)

    def _close_position(self, pos: Position, exit_price: float, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        pnl_usdt, pnl_pct = self.state.record_trade(
            self.symbol, pos.entry_time, now, pos.entry_price, exit_price, pos.qty, reason
        )
        self.state.clear_position(self.symbol)
        emoji = "🟢" if pnl_usdt >= 0 else "🔴"
        msg = (
            f"{emoji} *{self.symbol} POZİSYON KAPANDI* ({reason})\n"
            f"• Giriş: `${pos.entry_price:,.2f}` → Çıkış: `${exit_price:,.2f}`\n"
            f"• Miktar: `{pos.qty}`\n"
            f"• PnL: `{pnl_usdt:+,.2f} USDT ({pnl_pct:+.2f}%)`"
        )
        log.info("[%s] Pozisyon kapandı (%s): PnL %+.2f USDT (%+.2f%%)",
                 self.symbol, reason, pnl_usdt, pnl_pct)
        self.notifier.send(msg)

    # ------------------------------------------------------------------ stop yönetimi
    def _check_stop_order(self, pos: Position) -> Position | None:
        """Borsadaki stop emrinin durumunu kontrol eder.
        Pozisyon kapandıysa None, devam ediyorsa Position döndürür."""
        if pos.stop_order_id is None:
            return self._ensure_stop_exists(pos)

        status, fill = self.broker.order_status(self.symbol, pos.stop_order_id)
        if status == "FILLED" and fill:
            self._close_position(pos, fill.avg_price, "izleyen stop")
            return None
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            log.warning("[%s] Stop emri borsada %s durumda — yeniden kuruluyor", self.symbol, status)
            pos.stop_order_id = None
            self.state.save_position(pos)
            return self._ensure_stop_exists(pos)
        return pos  # NEW / PARTIALLY_FILLED / UNKNOWN(geçici ağ hatası)

    def _ensure_stop_exists(self, pos: Position) -> Position | None:
        """Stop emri yoksa (kurulum başarısız olmuşsa) tekrar dener.
        Bu, pozisyonun korumasız kalmaması için kritik güvenlik ağıdır."""
        qty = self._sellable_qty()
        if qty < self.filters.min_qty:
            # Elimizde satılacak coin yok → pozisyon borsada bir şekilde kapanmış
            log.warning("[%s] Bakiye yok; pozisyon dışarıdan kapanmış kabul ediliyor", self.symbol)
            price = self.market.last_price(self.symbol)
            self._close_position(pos, price, "mutabakat: bakiye yok")
            return None
        stop_p, limit_p = self._fmt_stop_prices(pos.trailing_stop)
        oid = self.broker.place_stop(self.symbol, qty, stop_p, limit_p)
        if oid is None:
            self.notifier.send_error(
                f"{self.symbol}: STOP EMRİ KURULAMADI! Pozisyon geçici korumasız. Tekrar denenecek."
            )
            return pos
        pos.stop_order_id = oid
        pos.qty = qty
        self.state.save_position(pos)
        log.info("[%s] Stop emri kuruldu: stop=%s qty=%s (id=%s)", self.symbol, stop_p, qty, oid)
        return pos

    def _raise_stop(self, pos: Position, new_stop: float) -> None:
        """İzleyen stopu yükseltir: eski emri iptal et, yenisini koy."""
        old_stop_p, _ = self._fmt_stop_prices(pos.trailing_stop)
        new_stop_p, new_limit_p = self._fmt_stop_prices(new_stop)
        if new_stop_p <= old_stop_p:
            return  # tick bazında anlamlı yükselme yok

        if pos.stop_order_id is not None:
            self.broker.cancel_order(self.symbol, pos.stop_order_id)
            pos.stop_order_id = None
            self.state.save_position(pos)  # iptal ile yeni emir arası çökmeye dayanıklı

        qty = self._sellable_qty()
        oid = self.broker.place_stop(self.symbol, qty, new_stop_p, new_limit_p)
        pos.trailing_stop = new_stop
        pos.stop_order_id = oid
        if qty >= self.filters.min_qty:
            pos.qty = qty
        self.state.save_position(pos)
        if oid is None:
            self.notifier.send_error(f"{self.symbol}: stop yükseltilirken yeni emir kurulamadı!")
        else:
            log.info("[%s] Stop yükseltildi: %s → %s", self.symbol, old_stop_p, new_stop_p)

    # ------------------------------------------------------------------ ana akış
    def poll(self) -> None:
        pos = self.state.get_position(self.symbol)

        # 1) Pozisyondaysak önce stop emrinin akıbetini kontrol et
        if pos is not None:
            pos = self._check_stop_order(pos)

        # 2) Yeni kapanmış mum var mı?
        df = self.market.klines(self.symbol, self.cfg.timeframe, limit=500)
        closed = df.iloc[:-1]  # son satır henüz kapanmamış mum — ASLA kullanılmaz
        if len(closed) < self.cfg.strategy.warmup_bars:
            log.warning("[%s] Yetersiz veri: %d bar", self.symbol, len(closed))
            return
        last_closed_time = int(closed.iloc[-1]["open_time"])
        if last_closed_time == self.state.get_last_candle(self.symbol):
            return  # bu mum zaten işlendi — mum başına tek karar
        self.state.set_last_candle(self.symbol, last_closed_time)

        ind = compute_indicators(closed, self.cfg.strategy)
        row = ind.iloc[-1]

        if pos is not None:
            self._on_candle_with_position(pos, row)
        else:
            self._on_candle_flat(row)

    def _on_candle_with_position(self, pos: Position, row: pd.Series) -> None:
        candle_high = float(row["high"])
        atr = float(row["atr"])
        if candle_high > pos.highest_price:
            pos.highest_price = candle_high
            self.state.save_position(pos)
        new_stop = updated_trailing_stop(pos.trailing_stop, pos.highest_price, atr, self.cfg.strategy)
        if new_stop > pos.trailing_stop:
            self._raise_stop(pos, new_stop)

    def _on_candle_flat(self, row: pd.Series) -> None:
        sig = check_entry(row, self.cfg.strategy)
        if not sig.should_enter:
            return

        # Devre kesici: günlük zarar limiti aşıldıysa yeni giriş yok
        today = datetime.now(timezone.utc).date().isoformat()
        equity = compute_equity(self.cfg, self.market, self.broker)
        allowed, day_pnl = self.breaker.entries_allowed(today, equity)
        if not allowed:
            log.warning("[%s] DEVRE KESİCİ AKTİF (günlük PnL %.1f%%) — giriş atlandı",
                        self.symbol, day_pnl * 100)
            self.notifier.send_error(
                f"Devre kesici: günlük zarar %{-day_pnl*100:.1f} sınırı aştı, yeni girişler durdu."
            )
            return

        usdt = self.broker.free_balance("USDT")
        stop_distance = sig.atr * self.cfg.strategy.atr_multiplier
        sizing = position_size(
            usdt, sig.close, stop_distance,
            self.cfg.risk_pct, self.cfg.max_balance_usage, self.filters,
        )
        if not sizing.ok:
            log.info("[%s] Sinyal var ama boyutlandırma engelledi: %s", self.symbol, sizing.reason)
            return

        fill = self.broker.market_buy(self.symbol, sizing.qty)
        if fill is None or fill.executed_qty <= 0:
            self.notifier.send_error(f"{self.symbol}: ALIM emri başarısız oldu.")
            return

        stop_price = initial_stop(fill.avg_price, sig.atr, self.cfg.strategy)
        pos = Position(
            symbol=self.symbol,
            qty=fill.executed_qty,
            entry_price=fill.avg_price,
            highest_price=fill.avg_price,
            trailing_stop=stop_price,
            stop_order_id=None,
            entry_time=datetime.now(timezone.utc).isoformat(),
        )
        self.state.save_position(pos)  # önce kaydet — stop kurulumu çökse bile pozisyon bilinir
        pos = self._ensure_stop_exists(pos)

        stop_txt = f"${pos.trailing_stop:,.2f}" if pos else "-"
        self.notifier.send(
            f"🟢 *{self.symbol} ALIM* ({sig.reason})\n"
            f"• Fiyat: `${fill.avg_price:,.2f}`  Miktar: `{fill.executed_qty}`\n"
            f"• İlk stop: `{stop_txt}` (ATR×{self.cfg.strategy.atr_multiplier})\n"
            f"• Kullanılan bakiye: `${fill.avg_price * fill.executed_qty:,.2f}`"
        )
        log.info("[%s] ALIM: fiyat=%.2f qty=%s stop=%.2f",
                 self.symbol, fill.avg_price, fill.executed_qty,
                 pos.trailing_stop if pos else float("nan"))

    # ------------------------------------------------------------------ mutabakat
    def reconcile(self) -> None:
        """Açılışta bot durumu ile borsa gerçeğini eşitler."""
        pos = self.state.get_position(self.symbol)
        if pos is None:
            # Durumda pozisyon yok ama borsada bizden kalma stop emri olabilir → iptal et
            for order in self.broker.open_orders(self.symbol):
                if order.get("side") == "SELL" and "STOP" in order.get("type", ""):
                    log.warning("[%s] Sahipsiz stop emri bulundu (id=%s) — iptal ediliyor",
                                self.symbol, order["orderId"])
                    self.broker.cancel_order(self.symbol, int(order["orderId"]))
            return

        log.info("[%s] Mutabakat: kayıtlı pozisyon bulundu (giriş=%.2f, stop=%.2f)",
                 self.symbol, pos.entry_price, pos.trailing_stop)
        result = self._check_stop_order(pos)
        if result is not None:
            self.notifier.send(
                f"♻️ *{self.symbol}*: bot yeniden başladı, açık pozisyon ve stop emri doğrulandı."
            )


# ---------------------------------------------------------------------- genel
def compute_equity(cfg: Config, market: MarketData, broker: Broker) -> float:
    equity = broker.free_balance("USDT")
    for symbol in cfg.symbols:
        qty = broker.base_balance(symbol)
        if qty > 0:
            try:
                equity += qty * market.last_price(symbol)
            except Exception as e:
                log.warning("Equity hesaplanırken %s fiyatı alınamadı: %s", symbol, e)
    return equity


def maybe_send_daily_report(cfg: Config, market: MarketData, broker: Broker,
                            state: StateStore, notifier: TelegramNotifier) -> None:
    now = datetime.now()  # yerel saat — kullanıcının belirlediği rapor saati yereldir
    today = now.date().isoformat()
    if now.hour < cfg.daily_report_hour or state.get_kv("last_report_date") == today:
        return
    state.set_kv("last_report_date", today)

    equity = compute_equity(cfg, market, broker)
    stats = state.trade_stats()
    day_pnl = state.todays_realized_pnl()
    open_lines = []
    for symbol in cfg.symbols:
        pos = state.get_position(symbol)
        if pos:
            price = market.last_price(symbol)
            upnl = (price - pos.entry_price) / pos.entry_price * 100
            open_lines.append(f"  · {symbol}: giriş `${pos.entry_price:,.2f}` anlık `%{upnl:+.2f}`")
    open_txt = "\n".join(open_lines) if open_lines else "  · Açık pozisyon yok"
    win_rate = (stats["wins"] / stats["count"] * 100) if stats["count"] else 0.0

    notifier.send(
        "📊 *GÜNLÜK ÖZET*\n"
        f"• Toplam varlık: `${equity:,.2f}`\n"
        f"• Bugün gerçekleşen PnL: `{day_pnl:+,.2f} USDT`\n"
        f"• Açık pozisyonlar:\n{open_txt}\n"
        f"• Toplam işlem: `{stats['count']}` | Kazanma: `%{win_rate:.1f}` "
        f"| Kümülatif PnL: `{stats['total_pnl']:+,.2f} USDT`\n"
        f"🤖 Mod: `{cfg.mode}`"
    )
