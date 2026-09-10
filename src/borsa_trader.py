"""Borsa (ABD + BIST) PAPER trader — kripto motorunun günlük-bar aynası.

SANAL PARA: gerçek hesap, gerçek emir, gerçek para YOKTUR. İki ayrı sanal
cüzdan tutulur çünkü iki piyasa iki para biriminde fiyatlanır ve bunları
toplamak yanıltıcı olur:
  - USD cüzdanı (ABD hisseleri/ETF'leri)  başlangıç: $10.000
  - TRY cüzdanı (BIST hisseleri)          başlangıç: ₺1.000.000

Kriptodan BİLİNÇLİ farklar:
  1. KALDIRAÇ YOK (1x). Hisse tarafında amaç stratejinin yeni varlık
     sınıfında çalışıp çalışmadığını görmek; kaldıraç o soruyu kirletmez.
  2. GAP'E DÜRÜST STOP: hisse gece kapalıyken fiyat stopun ÜZERİNDEN
     atlayabilir. Stop fiyatından değil, GERÇEKLEŞEN fiyattan çıkılır —
     kriptodaki "stop seviyesinden çıkış" varsayımı burada yalan olurdu.
  3. BIST'TE SHORT YOK. Bireysel yatırımcı için BIST'te açığa satış pratik
     değildir (ödünç havuzu, aracı kurum onayı); kağıtta simüle edip canlıda
     yapamayacağımız şeyi ölçmek kendini kandırmaktır. ABD'de short serbest.
  4. TAM HİSSE: miktarlar tam sayıya yuvarlanır (lot=1).
  5. FUNDING YOK (spot hisse), komisyon piyasaya göre farklı.

Değişmeyen ilkeler (kriptoyla birebir):
  - İncelemesiz giriş YOK: Donchian tetiği + 5 araçlı analiz onayı şart.
  - Stop-loss HER pozisyonda vardır, kaldırılamaz.
  - Tek yazıcı: pozisyonları yalnızca bu iş parçacığı değiştirir; panel
    komutu SQLite'a bırakır (bcmd_<SYM>), sonucu bcmdres_<SYM>'den okur.
  - Günlük devre kesici: cüzdan gün başına göre %5 eridiyse ZARARDAKI
    pozisyonlar kapanır, o gün yeni giriş yapılmaz. Kârdakiler yaşar.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import pandas as pd

from .analysis import analyzer_allows, assess
from .borsa_data import BorsaMarket, para_birimi
from .config import Config
from .notifier import TelegramNotifier
from .state import Position, StateStore
from .strategy import (check_entry, check_entry_short, compute_indicators,
                       updated_trailing_stop)

log = logging.getLogger("borsa")

SLIPPAGE = 0.001              # hisse spreadi kriptodan geniş
BASLANGIC = {"USD": 10_000.0, "TRY": 1_000_000.0}
MIN_TUTAR = {"USD": 50.0, "TRY": 1_000.0}
KOMISYON = {"USD": 0.0005, "TRY": 0.002}   # taraf başına; BIST'te BSMV dahil kaba
GUNLUK_KESICI = 0.05          # cüzdan gün başından %5 eridi → o gün dur
SIMGE = {"USD": "$", "TRY": "₺"}


class BorsaPaperTrader:
    """Tek sembolün günlük-bar paper döngüsü. Kriptodaki FuturesPaperTrader'ın aynası."""

    def __init__(self, symbol: str, cfg: Config, market: BorsaMarket,
                 state: StateStore, notifier: TelegramNotifier):
        self.symbol = symbol
        self.cfg = cfg
        self.market = market
        self.state = state
        self.notifier = notifier
        self.cur = para_birimi(symbol)
        if not self.state.get_kv(f"borsa_{self.cur}"):
            self.state.set_kv(f"borsa_{self.cur}", f"{BASLANGIC[self.cur]:.2f}")

    # ------------------------------------------------------------- sanal cüzdan
    def _balance(self) -> float:
        return float(self.state.get_kv(f"borsa_{self.cur}", str(BASLANGIC[self.cur])))

    def _set_balance(self, v: float) -> None:
        self.state.set_kv(f"borsa_{self.cur}", f"{v:.4f}")

    def _wallet_positions(self) -> list[Position]:
        return [p for p in self.state.all_positions() if para_birimi(p.symbol) == self.cur]

    def wallet_equity(self) -> float:
        eq = self._balance()
        for p in self._wallet_positions():
            price = self.market.last_price(p.symbol)
            eq += p.margin + (self._unrealized(p, price) if price else 0.0)
        return eq

    # ---------------------------------------------------------------- yardımcılar
    def _unrealized(self, pos: Position, price: float) -> float:
        if pos.side == "SHORT":
            return pos.qty * (pos.entry_price - price)
        return pos.qty * (price - pos.entry_price)

    def _close(self, pos: Position, exit_price: float, reason: str) -> None:
        raw = self._unrealized(pos, exit_price)
        exit_fee = pos.qty * exit_price * KOMISYON[self.cur]
        net = raw - pos.entry_fee_usdt - exit_fee
        self._set_balance(self._balance() + pos.margin + raw - exit_fee)

        now = datetime.now(timezone.utc).isoformat()
        self.state.record_trade(
            self.symbol, pos.entry_time, now, pos.entry_price, exit_price, pos.qty,
            reason, side=pos.side, pnl_override=net,
        )
        self.state.clear_position(self.symbol)
        s = SIMGE[self.cur]
        emoji = "🟢" if net >= 0 else "🔴"
        arrow = "📈 LONG" if pos.side == "LONG" else "📉 SHORT"
        self.notifier.send(
            f"{emoji} *BORSA · {self.symbol} {arrow} KAPANDI* ({reason})\n"
            f"• Giriş: `{s}{pos.entry_price:,.2f}` → Çıkış: `{s}{exit_price:,.2f}`\n"
            f"• Net PnL: `{net:+,.2f} {self.cur}` (komisyon dahil, sanal cüzdan)"
        )
        log.info("[%s] %s kapandı (%s): net %+.2f %s",
                 self.symbol, pos.side, reason, net, self.cur)

    def _stop_exit_price(self, pos: Position, price: float) -> float | None:
        """Stop tetiklendiyse GERÇEKLEŞEN çıkış fiyatı; tetiklenmediyse None.

        Gap dürüstlüğü: fiyat stopun ötesine ATLADIYSA stop fiyatı artık
        hayaldir — çıkış gördüğümüz fiyattan olur. Kriptoda (7/24 piyasa)
        stop seviyesinden çıkış varsayımı makuldü; burada değil.
        """
        if pos.side == "LONG":
            if price > pos.trailing_stop:
                return None
            return min(pos.trailing_stop, price) * (1 - SLIPPAGE)
        if price < pos.trailing_stop:
            return None
        return max(pos.trailing_stop, price) * (1 + SLIPPAGE)

    # --------------------------------------------------------- manuel komutlar
    def _sonuc(self, durum: str, mesaj: str) -> None:
        """Komut âkıbeti — panelin ⏳→✅/⛔ şeridi buradan okur (kriptoyla aynı desen)."""
        self.state.set_kv(
            f"bcmdres_{self.symbol}",
            f"{durum}|{datetime.now(timezone.utc).isoformat()}|{mesaj}")

    def _process_manual_commands(self, pos: Position | None,
                                 price: float | None) -> Position | None:
        key = f"bcmd_{self.symbol}"
        cmd = self.state.get_kv(key)
        if not cmd:
            return pos
        self.state.set_kv(key, "")   # hemen tüket — tekrar işlenmesin

        if cmd == "CLOSE":
            if pos is None:
                self._sonuc("red", "Kapatılacak açık pozisyon yok.")
                return None
            if price is None:
                # Fiyat yokken kapatmak "bilinmeyen fiyattan işlem" olur — reddet.
                # Komut kaybolmaz sanılmasın diye sonuç yazılır; kullanıcı tekrar dener.
                self._sonuc("red", "Şu an fiyat alınamıyor — kapatma ertelendi, tekrar dene.")
                return pos
            slip = (1 - SLIPPAGE) if pos.side == "LONG" else (1 + SLIPPAGE)
            log.info("[%s] MANUEL KAPATMA uygulanıyor", self.symbol)
            self._close(pos, price * slip, "manuel kapatma")
            self._sonuc("ok", f"Pozisyon kapatıldı @ {SIMGE[self.cur]}{price * slip:,.2f}")
            return None

        if cmd.startswith("STOP:"):
            return self._set_manual_stop(pos, price, cmd[5:])

        self._sonuc("red", f"Bilinmeyen komut: {cmd}")
        return pos

    def _set_manual_stop(self, pos: Position | None, price: float | None,
                         ham: str) -> Position | None:
        """Panelden manuel stop — kripto _set_manual_stop'un aynası.

        Aynı kurallar: seviye serbest (sıkma da gevşetme de), ama anında
        tetikleyecek seviye reddedilir ve stop hiçbir koşulda KALDIRILAMAZ.
        Gevşetmede iz sürme kilidi (stop_manual_ref) kriptodaki formülle
        kurulur: ref = 2×eski − yeni — işlem verdiğin payı geri kazanmadan
        algoritma kontrolü geri almaz.
        """
        s = SIMGE[self.cur]
        if pos is None:
            self._sonuc("red", "Stop değiştirilecek açık pozisyon yok.")
            return None
        if price is None:
            self._sonuc("red", "Şu an fiyat alınamıyor — stop değişikliği ertelendi, tekrar dene.")
            return pos
        try:
            yeni = float(ham)
        except ValueError:
            self._sonuc("red", f"Stop seviyesi sayıya çevrilemedi: {ham!r}")
            return pos
        if not (yeni > 0):
            self._sonuc("red", f"Stop 0 veya negatif olamaz ({yeni:g}) — reddedildi.")
            return pos

        tetikler = yeni >= price if pos.side == "LONG" else yeni <= price
        if tetikler:
            yon = "üstünde" if pos.side == "LONG" else "altında"
            self._sonuc("red", f"{s}{yeni:,.2f} anlık fiyatın ({yon} {s}{price:,.2f}) — "
                               f"pozisyonu anında kapatırdı. KAPAT düğmesini kullan.")
            return pos

        eski = pos.trailing_stop
        if abs(yeni - eski) < 1e-12:
            self._sonuc("bilgi", f"Stop zaten {s}{eski:,.2f} — değişiklik yok.")
            return pos
        sikilastir = yeni > eski if pos.side == "LONG" else yeni < eski

        pos.trailing_stop = yeni
        if sikilastir:
            pos.stop_manual_ref = 0.0     # cırcır zaten korur, kilit gerekmez
        else:
            ref = 2 * eski - yeni
            pos.stop_manual_ref = ref if ref > 0 else 1e-9
        self.state.save_position(pos)

        kilitli = pos.qty * ((yeni - pos.entry_price) if pos.side == "LONG"
                             else (pos.entry_price - yeni))
        log.info("[%s] MANUEL STOP %s: %.4f → %.4f (kilit ref=%.4f)",
                 self.symbol, "sıkıldı" if sikilastir else "GEVŞETİLDİ",
                 eski, yeni, pos.stop_manual_ref)
        self._sonuc("ok", f"Stop {'sıkıldı' if sikilastir else 'GEVŞETİLDİ'}: "
                          f"{s}{eski:,.2f} → {s}{yeni:,.2f} · garantilenen "
                          f"{kilitli:+,.2f} {self.cur}"
                          + ("" if sikilastir else " · iz sürme durduruldu 🔓"))
        self.notifier.send(
            f"{'🔒' if sikilastir else '⚠️'} *BORSA · {self.symbol} stop "
            f"{'sıkıldı' if sikilastir else 'GEVŞETİLDİ'}* (manuel)\n"
            f"• `{s}{eski:,.2f}` → `{s}{yeni:,.2f}` · garantilenen: "
            f"`{kilitli:+,.2f} {self.cur}`"
            + ("" if sikilastir else
               f"\n• İz sürme durdu — aday `{s}{pos.stop_manual_ref:,.2f}`'i "
               f"geçince kendiliğinden devam eder.")
        )
        return pos

    # ------------------------------------------------------- kâr kilometre taşı
    def _check_r_notify(self, pos: Position, price: float) -> None:
        """Pozisyon N×R kâra ulaşınca haber verir — KAPATMAZ (kriptoyla aynı ilke:
        otomatik kâr hedefi sağlamlık testini geçemedi, karar kullanıcıda)."""
        seviye = self.cfg.r_notify_level
        if seviye <= 0 or pos.qty <= 0:
            return
        if pos.risk_unit <= 0:
            tahmin = abs(pos.highest_price - pos.trailing_stop)
            if tahmin <= 0:
                return
            pos.risk_unit = tahmin
            self.state.save_position(pos)

        kar_mesafe = ((price - pos.entry_price) if pos.side == "LONG"
                      else (pos.entry_price - price))
        r = kar_mesafe / pos.risk_unit
        ulasilan = int(r / seviye) * seviye
        if ulasilan < seviye or ulasilan <= pos.r_notified:
            return

        upnl = self._unrealized(pos, price)
        kilitli = pos.qty * ((pos.trailing_stop - pos.entry_price) if pos.side == "LONG"
                             else (pos.entry_price - pos.trailing_stop))
        pos.r_notified = ulasilan
        self.state.save_position(pos)
        self.state.record_r_event(
            self.symbol, datetime.now(timezone.utc).isoformat(), pos.side,
            ulasilan, r, price, upnl, kilitli,
        )
        s = SIMGE[self.cur]
        self.notifier.send(
            f"🎯 *BORSA · {self.symbol} {r:.1f}R KÂRA ULAŞTI*\n"
            f"• Anlık kâr: `{upnl:+,.2f} {self.cur}`\n"
            f"• Stop kilidi: `{s}{pos.trailing_stop:,.2f}` → `{kilitli:+,.2f} {self.cur}` garantide\n"
            f"• Pozisyon DEVAM ediyor — kapatmak istersen panelden KAPAT."
        )
        log.info("[%s] %.1fR kâr bildirimi (upnl=%.2f kilitli=%.2f)",
                 self.symbol, r, upnl, kilitli)

    # ------------------------------------------------------- günlük devre kesici
    def _kesici_devrede(self) -> bool:
        """Cüzdan gün başına göre %5+ eridiyse True. Gün başı değeri ilk poll'da yazılır."""
        bugun = datetime.now(timezone.utc).date().isoformat()
        ham = self.state.get_kv(f"borsa_gunbasi_{self.cur}")
        if not ham.startswith(bugun):
            self.state.set_kv(f"borsa_gunbasi_{self.cur}", f"{bugun}|{self.wallet_equity():.4f}")
            return False
        try:
            gunbasi = float(ham.split("|", 1)[1])
        except (IndexError, ValueError):
            return False
        if gunbasi <= 0:
            return False
        return (self.wallet_equity() / gunbasi - 1.0) <= -GUNLUK_KESICI

    # ------------------------------------------------------------------ ana akış
    def poll(self) -> None:
        price = self.market.last_price(self.symbol)
        pos = self.state.get_position(self.symbol)
        pos = self._process_manual_commands(pos, price)

        if price is None:
            return   # veri yok → bu tur hiçbir karar yok (fiyatsız işlem olmaz)

        # Panel için son fiyat. Panel yfinance'e KENDİSİ sormaz (çifte trafik +
        # panelde ağ beklemesi); botun gördüğü fiyatı zamanıyla birlikte okur.
        self.state.set_kv(
            f"bfiyat_{self.symbol}",
            f"{price}|{datetime.now(timezone.utc).isoformat()}")

        # 1) Canlı stop kontrolü (gap'e dürüst)
        if pos is not None:
            cikis = self._stop_exit_price(pos, price)
            if cikis is not None:
                self._close(pos, cikis, "izleyen stop")
                pos = None
            else:
                self._check_r_notify(pos, price)

        # 1b) Günlük devre kesici — kriptodaki seçici kapatmanın aynısı:
        # yalnızca ZARARDAKI pozisyon kapanır, kârdaki izleyen stopla yaşar.
        if pos is not None and self._kesici_devrede():
            upnl = self._unrealized(pos, price)
            if upnl < 0:
                slip = (1 - SLIPPAGE) if pos.side == "LONG" else (1 + SLIPPAGE)
                self._close(pos, price * slip, "günlük sermaye stopu")
                self.notifier.send_error(
                    f"🛑 BORSA {self.cur} cüzdanı günlük kesici: {self.symbol} "
                    f"zararda olduğu için kapatıldı. Bugün yeni giriş yok.")
                pos = None

        # 2) Yeni kapanmış GÜNLÜK mum var mı?
        df = self.market.klines(self.symbol)
        if df is None:
            return
        closed = df.iloc[:-1]   # son bar seans içinde canlı olabilir — daima dışla
        if len(closed) < self.cfg.strategy.warmup_bars:
            return
        last_time = int(closed.iloc[-1]["open_time"])
        if last_time == self.state.get_last_candle(self.symbol):
            return
        self.state.set_last_candle(self.symbol, last_time)

        ind = compute_indicators(closed, self.cfg.strategy)
        row = ind.iloc[-1]

        # İNCELEMESİZ GİRİŞ YOK — kriptoyla aynı 5 araç, üst dilim = HAFTALIK.
        haftalik = self.market.haftalik_yukari(self.symbol)
        assessment = assess(self.symbol, row, self.cfg.strategy, haftalik)
        self.state.save_assessment(
            self.symbol, datetime.now(timezone.utc).isoformat(), assessment.to_json())
        log.info("[%s] Ön değerlendirme: %s (skor %+d)%s", self.symbol,
                 assessment.decision, assessment.score,
                 f" — {assessment.veto_reason}" if assessment.veto_reason else "")

        if pos is not None:
            self._update_trailing(pos, row)
        else:
            self._try_enter(row, assessment)

    def _update_trailing(self, pos: Position, row: pd.Series) -> None:
        atr = float(row["atr"])
        if pd.isna(atr):
            return
        if pos.side == "LONG":
            if float(row["high"]) > pos.highest_price:
                pos.highest_price = float(row["high"])
            aday = pos.highest_price - atr * self.cfg.strategy.atr_multiplier
            # Manuel gevşetme kilidi (kriptoyla aynı): işlem, gevşetme anındaki
            # adayı geçene kadar iz sürme DURUR — yoksa bot ertesi mumda geri alır.
            if pos.stop_manual_ref > 0:
                if aday <= pos.stop_manual_ref:
                    self.state.save_position(pos)   # yalnızca uç değeri kaydet
                    return
                log.info("[%s] Manuel stop kilidi açıldı (aday %.4f > ref %.4f)",
                         self.symbol, aday, pos.stop_manual_ref)
                pos.stop_manual_ref = 0.0
            new_stop = updated_trailing_stop(
                pos.trailing_stop, pos.highest_price, atr, self.cfg.strategy)
            if new_stop > pos.trailing_stop:
                log.info("[%s] LONG stop yükseltildi: %.2f → %.2f",
                         self.symbol, pos.trailing_stop, new_stop)
                pos.trailing_stop = new_stop
        else:
            if float(row["low"]) < pos.highest_price:
                pos.highest_price = float(row["low"])
            aday = pos.highest_price + atr * self.cfg.strategy.atr_multiplier
            if pos.stop_manual_ref > 0:
                if aday >= pos.stop_manual_ref:
                    self.state.save_position(pos)
                    return
                log.info("[%s] Manuel stop kilidi açıldı (aday %.4f < ref %.4f)",
                         self.symbol, aday, pos.stop_manual_ref)
                pos.stop_manual_ref = 0.0
            if aday < pos.trailing_stop:
                log.info("[%s] SHORT stop indirildi: %.2f → %.2f",
                         self.symbol, pos.trailing_stop, aday)
                pos.trailing_stop = aday
        self.state.save_position(pos)

    def _try_enter(self, row: pd.Series, assessment) -> None:
        side = ""
        sig = check_entry(row, self.cfg.strategy)
        if sig.should_enter:
            side = "LONG"
        if not side and self.cur == "USD":   # BIST'te short yok (modül notuna bak)
            sig = check_entry_short(row, self.cfg.strategy)
            if sig.should_enter:
                side = "SHORT"
        if not side:
            return
        if not analyzer_allows(assessment, side):
            log.info("[%s] %s kırılımı var ama analiz onaylamadı (%s, skor %+d) — RED",
                     self.symbol, side, assessment.decision, assessment.score)
            return
        if self._kesici_devrede():
            log.info("[%s] Günlük kesici aktif — giriş yok", self.symbol)
            return
        self._open(side, sig.atr, sig.reason)

    def _open(self, side: str, atr: float, reason: str) -> None:
        price = self.market.last_price(self.symbol)
        if price is None or pd.isna(atr) or atr <= 0:
            return

        # Cüzdan başına eşzamanlı pozisyon tavanı (kripto gerekçesiyle aynı:
        # korelasyonlu varlıklar tek büyük bahistir — sayıyı sınırla).
        diger = [p for p in self._wallet_positions() if p.symbol != self.symbol]
        if len(diger) >= self.cfg.borsa_max_positions:
            log.info("[%s] %s sinyali var ama %s cüzdanı tavanı dolu (%d) — ERTELENDİ",
                     self.symbol, side, self.cur, len(diger))
            return

        balance = self._balance()
        stop_distance = atr * self.cfg.strategy.atr_multiplier
        fill = price * (1 + SLIPPAGE) if side == "LONG" else price * (1 - SLIPPAGE)
        qty = math.floor(min(
            (balance * self.cfg.borsa_risk_pct) / stop_distance,
            (balance * 0.95) / fill,          # kaldıraç yok: tutar bakiyeyi aşamaz
        ))
        notional = qty * fill
        entry_fee = notional * KOMISYON[self.cur]

        if qty < 1 or notional < MIN_TUTAR[self.cur] or notional + entry_fee > balance:
            log.info("[%s] %s: boyut/bakiye yetersiz (qty=%d tutar=%.2f)",
                     self.symbol, side, qty, notional)
            return

        self._set_balance(balance - notional - entry_fee)
        stop = fill - stop_distance if side == "LONG" else fill + stop_distance
        pos = Position(
            symbol=self.symbol, qty=float(qty), entry_price=fill, highest_price=fill,
            trailing_stop=stop, stop_order_id=None,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_fee_usdt=entry_fee, side=side,
            margin=notional,               # 1x: kilitli tutar = pozisyon tutarı
            funding_acc=0.0, risk_unit=stop_distance,
        )
        self.state.save_position(pos)
        s = SIMGE[self.cur]
        arrow = "📈 LONG" if side == "LONG" else "📉 SHORT"
        self.notifier.send(
            f"{arrow} *BORSA · {self.symbol} AÇILDI* ({reason})\n"
            f"• Giriş: `{s}{fill:,.2f}`  Adet: `{qty}`  Tutar: `{s}{notional:,.2f}`\n"
            f"• Stop: `{s}{stop:,.2f}` (sanal cüzdan, {self.cur})"
        )
        log.info("[%s] %s açıldı: fiyat=%.2f adet=%d stop=%.2f",
                 self.symbol, side, fill, qty, stop)
