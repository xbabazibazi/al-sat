"""Vadeli işlem PAPER trading motoru — gerçek fiyat, sanal marjin hesabı.

Gerçek para RİSK EDİLMEZ: emirler yerel simülasyondur ama fiyatlar, komisyonlar
(%0.05 taker), funding tahakkuku ve marjin muhasebesi gerçek USDT-M kurallarına
göre işler. Backtest'le birebir aynı strateji modülünü kullanır.

Long + short iki yön de desteklenir (varsayılan: yalnızca long — backtest kanıtı
short'un zarar ettiğini gösterdi; .env ALLOW_SHORT=true ile açılır ve panelde
sonuçları risksiz izlenir).

Stop mantığı: paper modda "borsa" bu süreçtir — stop her poll'da canlı fiyatla
kontrol edilir. Durum SQLite'ta kalıcıdır; yeniden başlatma pozisyonu unutturmaz.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from .analysis import analyzer_allows, assess
from .config import Config
from .exchange import MarketData, floor_to_step
from .notifier import TelegramNotifier
from .risk import CircuitBreaker
from .state import Position, StateStore
from .strategy import (check_entry, check_entry_short, compute_indicators, ema,
                       updated_trailing_stop)

log = logging.getLogger("futures")

SLIPPAGE = 0.0005


class FuturesPaperTrader:
    def __init__(self, symbol: str, cfg: Config, market: MarketData,
                 state: StateStore, notifier: TelegramNotifier, breaker: CircuitBreaker):
        self.symbol = symbol
        self.cfg = cfg
        self.market = market
        self.state = state
        self.notifier = notifier
        self.breaker = breaker
        self.filters = market.filters(symbol)
        if not self.state.get_kv("fut_usdt"):
            self.state.set_kv("fut_usdt", "10000.0")

    # ------------------------------------------------------------- sanal cüzdan
    def _balance(self) -> float:
        return float(self.state.get_kv("fut_usdt", "10000.0"))

    def _set_balance(self, v: float) -> None:
        self.state.set_kv("fut_usdt", f"{v:.8f}")

    # ---------------------------------------------------------------- yardımcılar
    def _unrealized(self, pos: Position, price: float) -> float:
        if pos.side == "SHORT":
            return pos.qty * (pos.entry_price - price)
        return pos.qty * (price - pos.entry_price)

    def _close(self, pos: Position, exit_price: float, reason: str) -> None:
        raw = self._unrealized(pos, exit_price)
        exit_fee = pos.qty * exit_price * self.cfg.futures_taker_fee
        net = raw - pos.entry_fee_usdt - exit_fee - pos.funding_acc
        self._set_balance(self._balance() + pos.margin + raw - exit_fee - pos.funding_acc)

        now = datetime.now(timezone.utc).isoformat()
        pnl_usdt, pnl_pct = self.state.record_trade(
            self.symbol, pos.entry_time, now, pos.entry_price, exit_price, pos.qty,
            reason, side=pos.side, pnl_override=net,
        )
        self.state.clear_position(self.symbol)
        emoji = "🟢" if net >= 0 else "🔴"
        arrow = "📈 LONG" if pos.side == "LONG" else "📉 SHORT"
        self.notifier.send(
            f"{emoji} *{self.symbol} {arrow} KAPANDI* ({reason})\n"
            f"• Giriş: `${pos.entry_price:,.2f}` → Çıkış: `${exit_price:,.2f}`\n"
            f"• Net PnL: `{net:+,.2f} USDT` (komisyon+funding dahil)"
        )
        log.info("[%s] %s kapandı (%s): net %+.2f USDT", self.symbol, pos.side, reason, net)

    def _stop_hit(self, pos: Position, price: float) -> bool:
        return price <= pos.trailing_stop if pos.side == "LONG" else price >= pos.trailing_stop

    # ------------------------------------------------------------------ ana akış
    def poll(self) -> None:
        price = self.market.last_price(self.symbol)
        pos = self.state.get_position(self.symbol)

        # 1) Funding tahakkuku + canlı stop kontrolü (her poll)
        if pos is not None:
            rate = (self.cfg.funding_daily_long if pos.side == "LONG"
                    else self.cfg.funding_daily_short)
            pos.funding_acc += pos.qty * pos.entry_price * rate * (self.cfg.poll_seconds / 86400.0)
            self.state.save_position(pos)

            if self._stop_hit(pos, price):
                slip = (1 - SLIPPAGE) if pos.side == "LONG" else (1 + SLIPPAGE)
                self._close(pos, pos.trailing_stop * slip, "izleyen stop")
                pos = None

        # 1b) GÜNLÜK SERMAYE STOP'U: günlük zarar limiti aşıldıysa pozisyonları
        # kapat ve günü bitir (yalnızca yeni girişleri kesmekle kalmaz).
        if pos is not None:
            today = datetime.now(timezone.utc).date().isoformat()
            allowed, day_pnl = self.breaker.entries_allowed(today, self.account_equity())
            if not allowed:
                slip = (1 - SLIPPAGE) if pos.side == "LONG" else (1 + SLIPPAGE)
                self._close(pos, price * slip, "günlük sermaye stopu")
                self.notifier.send_error(
                    f"🛑 GÜNLÜK SERMAYE STOPU: zarar %{-day_pnl*100:.1f} limiti aştı — "
                    f"{self.symbol} kapatıldı, bugün yeni giriş yok."
                )
                pos = None

        # 2) Yeni kapanmış mum
        df = self.market.klines(self.symbol, self.cfg.timeframe, limit=500)
        closed = df.iloc[:-1]
        if len(closed) < self.cfg.strategy.warmup_bars:
            return
        last_time = int(closed.iloc[-1]["open_time"])
        if last_time == self.state.get_last_candle(self.symbol):
            return
        self.state.set_last_candle(self.symbol, last_time)

        ind = compute_indicators(closed, self.cfg.strategy)
        row = ind.iloc[-1]

        # ÖN DEĞERLENDİRME: her mum kapanışında 5 araç çalışır ve kaydedilir —
        # incelemesiz hiçbir işleme girilmez, karar panelde her an görünür.
        assessment = assess(self.symbol, row, self.cfg.strategy, self._daily_uptrend())
        self.state.save_assessment(
            self.symbol, datetime.now(timezone.utc).isoformat(), assessment.to_json()
        )
        log.info("[%s] Ön değerlendirme: %s (skor %+d)%s", self.symbol,
                 assessment.decision, assessment.score,
                 f" — {assessment.veto_reason}" if assessment.veto_reason else "")

        if pos is not None:
            self._update_trailing(pos, row)
        else:
            self._try_enter(row, assessment)

    def _daily_uptrend(self) -> bool | None:
        """Günlük trend oyu için veri; alınamazsa None (araç oy kullanmaz)."""
        try:
            d = self.market.klines(self.symbol, "1d", limit=400)
            d_closed = d.iloc[:-1]
            if len(d_closed) < self.cfg.strategy.daily_ema_period:
                return None
            daily_ema = ema(d_closed["close"], self.cfg.strategy.daily_ema_period)
            return float(d_closed["close"].iloc[-1]) > float(daily_ema.iloc[-1])
        except Exception as e:
            log.warning("[%s] Günlük veri alınamadı: %s", self.symbol, e)
            return None

    def _update_trailing(self, pos: Position, row: pd.Series) -> None:
        atr = float(row["atr"])
        if pd.isna(atr):
            return
        if pos.side == "LONG":
            if float(row["high"]) > pos.highest_price:
                pos.highest_price = float(row["high"])
            new_stop = updated_trailing_stop(pos.trailing_stop, pos.highest_price, atr, self.cfg.strategy)
            if new_stop > pos.trailing_stop:
                log.info("[%s] LONG stop yükseltildi: %.2f → %.2f", self.symbol, pos.trailing_stop, new_stop)
                pos.trailing_stop = new_stop
        else:
            if float(row["low"]) < pos.highest_price:  # short'ta uç değer = en düşük
                pos.highest_price = float(row["low"])
            new_stop = min(pos.trailing_stop,
                           pos.highest_price + atr * self.cfg.strategy.atr_multiplier)
            if new_stop < pos.trailing_stop:
                log.info("[%s] SHORT stop indirildi: %.2f → %.2f", self.symbol, pos.trailing_stop, new_stop)
                pos.trailing_stop = new_stop
        self.state.save_position(pos)

    def _try_enter(self, row: pd.Series, assessment) -> None:
        side = ""
        if self.cfg.allow_long:
            sig = check_entry(row, self.cfg.strategy)
            if sig.should_enter:
                side = "LONG"
        if not side and self.cfg.allow_short:
            sig = check_entry_short(row, self.cfg.strategy)
            if sig.should_enter:
                side = "SHORT"
        if not side:
            return

        # İNCELEMESİZ GİRİŞ YOK: kırılım sinyali olsa bile ön değerlendirme
        # bu yönü onaylamadıysa işlem reddedilir.
        if not analyzer_allows(assessment, side):
            log.info("[%s] %s kırılımı var ama analiz onaylamadı (%s, skor %+d) — GİRİŞ REDDEDİLDİ",
                     self.symbol, side, assessment.decision, assessment.score)
            return

        today = datetime.now(timezone.utc).date().isoformat()
        equity = self.account_equity()
        allowed, day_pnl = self.breaker.entries_allowed(today, equity)
        if not allowed:
            log.warning("[%s] Devre kesici aktif (günlük %%%.1f) — giriş yok",
                        self.symbol, day_pnl * 100)
            return

        balance = self._balance()
        stop_distance = sig.atr * self.cfg.strategy.atr_multiplier
        if stop_distance <= 0:
            return
        qty = min((balance * self.cfg.risk_pct) / stop_distance,
                  (balance * self.cfg.max_balance_usage * self.cfg.leverage) / sig.close)
        qty = floor_to_step(qty, self.filters.step_size)
        price = self.market.last_price(self.symbol)
        fill = price * (1 + SLIPPAGE) if side == "LONG" else price * (1 - SLIPPAGE)
        notional = qty * fill
        margin = notional / self.cfg.leverage
        entry_fee = notional * self.cfg.futures_taker_fee

        if qty < self.filters.min_qty or notional < 10 or margin + entry_fee > balance:
            log.info("[%s] %s sinyali var ama boyut/bakiye yetersiz", self.symbol, side)
            return

        self._set_balance(balance - margin - entry_fee)
        stop = fill - stop_distance if side == "LONG" else fill + stop_distance
        pos = Position(
            symbol=self.symbol, qty=qty, entry_price=fill, highest_price=fill,
            trailing_stop=stop, stop_order_id=None,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_fee_usdt=entry_fee, side=side, margin=margin, funding_acc=0.0,
        )
        self.state.save_position(pos)
        arrow = "📈 LONG" if side == "LONG" else "📉 SHORT"
        self.notifier.send(
            f"{arrow} *{self.symbol} AÇILDI* ({sig.reason})\n"
            f"• Giriş: `${fill:,.2f}`  Miktar: `{qty}`  Kaldıraç: `{self.cfg.leverage}x`\n"
            f"• Marjin: `${margin:,.2f}`  Stop: `${stop:,.2f}`"
        )
        log.info("[%s] %s açıldı: fiyat=%.2f qty=%s stop=%.2f marjin=%.2f",
                 self.symbol, side, fill, qty, stop, margin)

    # ------------------------------------------------------------------ hesap değeri
    def position_equity(self, pos: Position, price: float) -> float:
        return pos.margin + self._unrealized(pos, price) - pos.funding_acc

    def account_equity(self) -> float:
        equity = self._balance()
        for p in self.state.all_positions():
            try:
                price = self.market.last_price(p.symbol)
                equity += p.margin + self._unrealized(p, price) - p.funding_acc
            except Exception as e:
                log.warning("Equity: %s fiyatı alınamadı (%s)", p.symbol, e)
                equity += p.margin
        return equity


def snapshot_equity(traders: list[FuturesPaperTrader], state: StateStore) -> None:
    """Panel grafiği için varlık anlık görüntüsü (dakikada bir yeterli)."""
    if not traders:
        return
    now = datetime.now(timezone.utc)
    key = now.strftime("%Y-%m-%dT%H:%M")  # dakika çözünürlüğü — PK çakışması update olur
    state.record_equity(key, traders[0].account_equity())


def maybe_send_futures_daily_report(cfg, traders: list[FuturesPaperTrader],
                                    state: StateStore, notifier) -> None:
    """Vadeli paper modda günlük Telegram özeti — haftalık takip için."""
    if not traders:
        return
    now = datetime.now()  # rapor saati yereldir
    today = now.date().isoformat()
    if now.hour < cfg.daily_report_hour or state.get_kv("last_fut_report") == today:
        return
    state.set_kv("last_fut_report", today)

    equity = traders[0].account_equity()
    stats = state.trade_stats()
    day_pnl = state.todays_realized_pnl()
    win_rate = (stats["wins"] / stats["count"] * 100) if stats["count"] else 0.0

    lines = []
    for t in traders:
        pos = state.get_position(t.symbol)
        if pos:
            try:
                price = t.market.last_price(t.symbol)
                upnl = t._unrealized(pos, price) - pos.funding_acc
                arrow = "📈" if pos.side == "LONG" else "📉"
                lines.append(f"  {arrow} {t.symbol} {pos.side}: `{upnl:+,.2f} USDT`")
            except Exception:
                lines.append(f"  · {t.symbol} {pos.side}: fiyat alınamadı")
    open_txt = "\n".join(lines) if lines else "  · Açık pozisyon yok"

    notifier.send(
        "📊 *GÜNLÜK ÖZET*\n"
        f"• Toplam varlık: `${equity:,.2f}` "
        f"(`{equity - 10000:+,.2f}` / `{(equity/10000-1)*100:+.2f}%`)\n"
        f"• Bugün gerçekleşen: `{day_pnl:+,.2f} USDT`\n"
        f"• Açık pozisyonlar:\n{open_txt}\n"
        f"• Toplam işlem: `{stats['count']}` · kazanma `%{win_rate:.0f}` "
        f"· kümülatif `{stats['total_pnl']:+,.2f} USDT`\n"
        f"🤖 `{cfg.mode}` · {cfg.leverage:.0f}x · "
        f"{'long+short' if cfg.allow_short else 'long'}"
    )
