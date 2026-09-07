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

    # ------------------------------------------------------- kâr kilometre taşı
    def _check_r_notify(self, pos: Position, price: float) -> None:
        """Pozisyon N×R kâra ulaştığında Telegram'a haber verir — KAPATMAZ.

        Otomatik kâr hedefi bilerek konulmadı: mevcut parite listesiyle
        sağlamlık testini geçemedi. Karar kullanıcıda; bildirim gelir,
        dilerse panelden KAPAT'a basar, basmazsa izleyen stop işine devam eder.

        Bildirimde 'kilitli kâr' de gösterilir çünkü asıl güven verici bilgi
        odur: 3×ATR izleyen stop ile 4R'ye ulaşıldığında stop zaten giriş+3R
        civarındadır, yani kârın büyük kısmı garanti altındadır.
        """
        seviye = self.cfg.r_notify_level
        if seviye <= 0 or pos.qty <= 0:
            return

        # Bu özellikten ÖNCE açılmış pozisyonlarda risk_unit kayıtlı değil.
        # Uydurmak yerine mevcut izleyen stop mesafesinden türetilir; bu,
        # giriş anındaki ATR ile bugünkü ATR aynıysa birebir, değilse
        # yaklaşıktır (bu yüzden mesajda "≈" kullanılır).
        yaklasik = False
        if pos.risk_unit <= 0:
            tahmin = abs(pos.highest_price - pos.trailing_stop)
            if tahmin <= 0:
                return
            pos.risk_unit = tahmin
            yaklasik = True
            self.state.save_position(pos)

        kar_mesafe = ((price - pos.entry_price) if pos.side == "LONG"
                      else (pos.entry_price - price))
        r = kar_mesafe / pos.risk_unit
        ulasilan = int(r / seviye) * seviye          # 4R, 8R, 12R...
        if ulasilan < seviye or ulasilan <= pos.r_notified:
            return

        upnl = self._unrealized(pos, price) - pos.funding_acc
        kilitli = pos.qty * ((pos.trailing_stop - pos.entry_price) if pos.side == "LONG"
                             else (pos.entry_price - pos.trailing_stop))
        pos.r_notified = ulasilan
        self.state.save_position(pos)
        self.state.record_r_event(
            self.symbol, datetime.now(timezone.utc).isoformat(), pos.side,
            ulasilan, r, price, upnl, kilitli,
        )

        isaret = "≈" if yaklasik else ""
        self.notifier.send(
            f"🎯 *{self.symbol} {isaret}{r:.1f}R KÂRA ULAŞTI*\n"
            f"• Anlık kâr: `{upnl:+,.2f}` USDT\n"
            f"• Stop kilidi: `${pos.trailing_stop:,.6g}` → `{kilitli:+,.2f}` USDT garantide\n"
            f"• Pozisyon DEVAM ediyor — kapatmak istersen panelden KAPAT."
        )
        log.info("[%s] %.1fR kâr bildirimi gönderildi (upnl=%.2f kilitli=%.2f)",
                 self.symbol, r, upnl, kilitli)

    # --------------------------------------------------------- manuel komutlar
    def _process_manual_commands(self, pos: Position | None, price: float) -> Position | None:
        """Panelden gelen manuel AÇ/KAPAT komutlarını işler.

        Panel doğrudan pozisyona dokunmaz; komutu SQLite'a yazar, bot burada
        uygular. Böylece pozisyon/bakiye üzerinde tek yazıcı bot kalır
        (iki süreç aynı anda yazarsa tutarsızlık olurdu).
        """
        key = f"cmd_{self.symbol}"
        cmd = self.state.get_kv(key)
        if not cmd:
            return pos
        self.state.set_kv(key, "")  # komutu hemen tüket (tekrar işlenmesin)

        if cmd == "CLOSE":
            if pos is None:
                log.info("[%s] Manuel kapatma istendi ama açık pozisyon yok", self.symbol)
                return None
            slip = (1 - SLIPPAGE) if pos.side == "LONG" else (1 + SLIPPAGE)
            log.info("[%s] MANUEL KAPATMA uygulanıyor", self.symbol)
            self._close(pos, price * slip, "manuel kapatma")
            return None

        if cmd.startswith("STOP:"):
            return self._set_manual_stop(pos, price, cmd[5:])

        if cmd in ("OPEN_LONG", "OPEN_SHORT"):
            if pos is not None:
                log.info("[%s] Manuel açma istendi ama zaten pozisyon var", self.symbol)
                return pos
            side = "LONG" if cmd == "OPEN_LONG" else "SHORT"
            log.info("[%s] MANUEL %s açılıyor", self.symbol, side)
            self._open(side, manual=True)
            return self.state.get_position(self.symbol)

        return pos

    def _set_manual_stop(self, pos: Position | None, price: float, ham: str) -> Position | None:
        """Panelden gelen manuel stop seviyesini uygular.

        Kural: stop seviyesi SERBEST (sıkabilir de gevşetebilir de), ama
        pozisyonu ANINDA tetikleyecek bir seviye reddedilir — o "kapat"
        demektir ve bunun için KAPAT düğmesi var. Stop'un tamamen kaldırılması
        hiçbir koşulda mümkün değil ("stop loss daima olacak").
        """
        if pos is None:
            log.info("[%s] Manuel stop istendi ama açık pozisyon yok", self.symbol)
            return None
        try:
            yeni = float(ham)
        except ValueError:
            log.warning("[%s] Manuel stop okunamadı: %r", self.symbol, ham)
            return pos
        if not (yeni > 0):
            self.notifier.send_error(f"{self.symbol}: stop 0 veya negatif olamaz — reddedildi.")
            return pos

        # anında tetiklenir mi?
        tetikler = yeni >= price if pos.side == "LONG" else yeni <= price
        if tetikler:
            self.notifier.send_error(
                f"⛔️ {self.symbol}: stop `${yeni:,.6g}` anlık fiyatın "
                f"({'üstünde' if pos.side == 'LONG' else 'altında'}, `${price:,.6g}`) — "
                f"bu pozisyonu anında kapatır. İstediğin buysa KAPAT düğmesini kullan."
            )
            return pos

        eski = pos.trailing_stop
        if abs(yeni - eski) < 1e-12:
            return pos
        sikilastir = yeni > eski if pos.side == "LONG" else yeni < eski

        pos.trailing_stop = yeni
        if sikilastir:
            # Cırcır mantığı zaten daha sıkı stop'u korur; kilide gerek yok.
            pos.stop_manual_ref = 0.0
        else:
            # Gevşetme: iz sürmeyi durdur, yoksa bot bir sonraki mumda geri alır.
            # Kilit ne zaman açılsın? "Eski seviyeyi geçince" DEĞİL — o zaman
            # 1 kuruşluk yeni tepe bile izni iptal eder ve tam kaçınmak istediğin
            # yerde stop yersin. Doğrusu: işlem VERDİĞİN NEFES PAYINI GERİ
            # KAZANMALI. ref = eski + (eski − yeni) = 2×eski − yeni (iki yön için
            # de aynı formül). Ne kadar çok pay istediysen, algoritmanın kontrolü
            # geri alması için işlemin o kadar çok ilerlemesi gerekir.
            ref = 2 * eski - yeni
            pos.stop_manual_ref = ref if ref > 0 else 1e-9  # 0 = "kilit yok" nöbetçisi
        self.state.save_position(pos)

        risk = pos.qty * (price - yeni if pos.side == "LONG" else yeni - price)
        kilitli = pos.qty * ((yeni - pos.entry_price) if pos.side == "LONG"
                             else (pos.entry_price - yeni))
        log.info("[%s] MANUEL STOP %s: %.6g → %.6g (kilit ref=%.6g)",
                 self.symbol, "sıkıldı" if sikilastir else "GEVŞETİLDİ",
                 eski, yeni, pos.stop_manual_ref)
        if sikilastir:
            self.notifier.send(
                f"🔒 *{self.symbol} stop sıkıldı* (manuel)\n"
                f"• `${eski:,.6g}` → `${yeni:,.6g}`\n"
                f"• Garantilenen: `{kilitli:+,.2f}` USDT · risktekiler: `{risk:,.2f}` USDT\n"
                f"• İz süren stop buradan yukarı devam eder."
            )
        else:
            self.notifier.send(
                f"⚠️ *{self.symbol} stop GEVŞETİLDİ* (manuel)\n"
                f"• `${eski:,.6g}` → `${yeni:,.6g}`\n"
                f"• Garantilenen: `{kilitli:+,.2f}` USDT · risktekiler: `{risk:,.2f}` USDT\n"
                f"• İz süren stop DURDURULDU — işlem verdiğin payı geri kazanıp "
                f"aday `${pos.stop_manual_ref:,.6g}`'i geçtiğinde kendiliğinden devam eder."
            )
        return pos

    # ------------------------------------------------------------------ ana akış
    def poll(self) -> None:
        price = self.market.last_price(self.symbol)
        pos = self.state.get_position(self.symbol)
        pos = self._process_manual_commands(pos, price)

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
            else:
                # Kâr kilometre taşı bildirimi (stop çalışmadıysa anlamlı)
                self._check_r_notify(pos, price)

        # 1b) GÜNLÜK SERMAYE STOP'U — SEÇİCİ KAPATMA
        # Limit aşıldığında YALNIZCA zarardaki pozisyonlar kapatılır.
        # Kârdaki pozisyonlar açık kalır çünkü:
        #   - stratejinin kârı, kazanan işlemlerin uzun sürmesinden gelir
        #   - kazanan pozisyon "suçlu" değil; başkası kaybetti diye kesmek mantıksız
        #   - zaten izleyen stop'u var, kâr geri verilse bile korumalı
        # Yeni girişler her hâlükârda durur (breaker.entries_allowed).
        if pos is not None:
            today = datetime.now(timezone.utc).date().isoformat()
            allowed, day_pnl = self.breaker.entries_allowed(today, self.account_equity())
            if not allowed:
                upnl = self._unrealized(pos, price) - pos.funding_acc
                if upnl < 0:
                    slip = (1 - SLIPPAGE) if pos.side == "LONG" else (1 + SLIPPAGE)
                    self._close(pos, price * slip, "günlük sermaye stopu")
                    self.notifier.send_error(
                        f"🛑 GÜNLÜK SERMAYE STOPU (%{-day_pnl*100:.1f}) — "
                        f"{self.symbol} ZARARDA olduğu için kapatıldı. Bugün yeni giriş yok."
                    )
                    pos = None
                else:
                    log.info("[%s] Devre kesici aktif ama pozisyon KÂRDA (%+.2f USDT) — "
                             "açık bırakılıyor, izleyen stop koruyor", self.symbol, upnl)

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
            aday = pos.highest_price - atr * self.cfg.strategy.atr_multiplier
            # Manuel gevşetme kilidi: işlem, gevşetme anındaki adayı geçene kadar
            # dokunma. Geçtiyse kilidi aç ve normal iz sürmeye dön.
            if pos.stop_manual_ref > 0:
                if aday <= pos.stop_manual_ref:
                    self.state.save_position(pos)   # yalnızca uç değeri kaydet
                    return
                log.info("[%s] Manuel stop kilidi açıldı (aday %.6g > ref %.6g)",
                         self.symbol, aday, pos.stop_manual_ref)
                pos.stop_manual_ref = 0.0
            new_stop = updated_trailing_stop(pos.trailing_stop, pos.highest_price, atr, self.cfg.strategy)
            if new_stop > pos.trailing_stop:
                log.info("[%s] LONG stop yükseltildi: %.2f → %.2f", self.symbol, pos.trailing_stop, new_stop)
                pos.trailing_stop = new_stop
        else:
            if float(row["low"]) < pos.highest_price:  # short'ta uç değer = en düşük
                pos.highest_price = float(row["low"])
            aday = pos.highest_price + atr * self.cfg.strategy.atr_multiplier
            if pos.stop_manual_ref > 0:
                if aday >= pos.stop_manual_ref:
                    self.state.save_position(pos)
                    return
                log.info("[%s] Manuel stop kilidi açıldı (aday %.6g < ref %.6g)",
                         self.symbol, aday, pos.stop_manual_ref)
                pos.stop_manual_ref = 0.0
            new_stop = min(pos.trailing_stop, aday)
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

        self._open(side, atr=sig.atr, reason=sig.reason)

    def _open(self, side: str, atr: float | None = None,
              reason: str = "", manual: bool = False) -> None:
        """Pozisyon açar. Otomatik sinyalde de manuel komutta da burası çalışır —
        risk yönetimi, stop kurulumu ve devre kesici HER İKİSİNDE de geçerli."""
        if atr is None:  # manuel açılışta ATR'yi taze veriden hesapla
            df = self.market.klines(self.symbol, self.cfg.timeframe, limit=300)
            ind = compute_indicators(df.iloc[:-1], self.cfg.strategy)
            atr = float(ind.iloc[-1]["atr"])
            if pd.isna(atr) or atr <= 0:
                log.warning("[%s] Manuel açılış: ATR hesaplanamadı", self.symbol)
                return

        today = datetime.now(timezone.utc).date().isoformat()
        equity = self.account_equity()
        allowed, day_pnl = self.breaker.entries_allowed(today, equity)
        if not allowed:
            log.warning("[%s] Devre kesici aktif (günlük %%%.1f) — giriş yok",
                        self.symbol, day_pnl * 100)
            if manual:
                self.notifier.send_error(
                    f"{self.symbol}: manuel açılış reddedildi — günlük sermaye stopu aktif."
                )
            return

        # PORTFÖY KORUMASI: eşzamanlı pozisyon tavanı (gerekçe: Config'te).
        # Korelasyon 0.68 olduğu için sınırsız eşzamanlı pozisyon "10 ayrı %1
        # risk" değil, tek yönde ~%8.4 risk demektir. Manuel açılış engellenmez
        # ama uyarılır — manuel, kullanıcının bilinçli kararıdır.
        tavan = self.cfg.max_concurrent_positions
        if tavan > 0:
            diger = [p for p in self.state.all_positions() if p.symbol != self.symbol]
            if len(diger) >= tavan:
                if manual:
                    self.notifier.send_error(
                        f"⚠️ {self.symbol} manuel açılıyor ama zaten {len(diger)} pozisyon "
                        f"açık (tavan {tavan}) — portföy riski tavanın üstüne çıkıyor."
                    )
                else:
                    log.info("[%s] %s sinyali var ama eşzamanlı pozisyon tavanı dolu "
                             "(%d/%d) — GİRİŞ ERTELENDİ",
                             self.symbol, side, len(diger), tavan)
                    return

        balance = self._balance()
        stop_distance = atr * self.cfg.strategy.atr_multiplier
        if stop_distance <= 0:
            return
        ref_price = self.market.last_price(self.symbol)
        qty = min((balance * self.cfg.risk_pct) / stop_distance,
                  (balance * self.cfg.max_balance_usage * self.cfg.leverage) / ref_price)
        qty = floor_to_step(qty, self.filters.step_size)
        fill = ref_price * (1 + SLIPPAGE) if side == "LONG" else ref_price * (1 - SLIPPAGE)
        notional = qty * fill
        margin = notional / self.cfg.leverage
        entry_fee = notional * self.cfg.futures_taker_fee

        if qty < self.filters.min_qty or notional < 10 or margin + entry_fee > balance:
            log.info("[%s] %s: boyut/bakiye yetersiz (qty=%s notional=%.2f)",
                     self.symbol, side, qty, notional)
            if manual:
                self.notifier.send_error(f"{self.symbol}: manuel açılış — bakiye/miktar yetersiz.")
            return

        self._set_balance(balance - margin - entry_fee)
        stop = fill - stop_distance if side == "LONG" else fill + stop_distance
        pos = Position(
            symbol=self.symbol, qty=qty, entry_price=fill, highest_price=fill,
            trailing_stop=stop, stop_order_id=None,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_fee_usdt=entry_fee, side=side, margin=margin, funding_acc=0.0,
            risk_unit=stop_distance,   # 1R — kâr bildirimi bunun katlarına bakar
        )
        self.state.save_position(pos)
        arrow = "📈 LONG" if side == "LONG" else "📉 SHORT"
        etiket = "🖐 MANUEL" if manual else reason
        self.notifier.send(
            f"{arrow} *{self.symbol} AÇILDI* ({etiket})\n"
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
