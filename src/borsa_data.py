"""Borsa (ABD + BIST) veri katmanı — yfinance üzerinden GÜNLÜK mumlar.

Kripto tarafındaki MarketData'nın hisse karşılığı. Farklar bilinçli:

  - Zaman dilimi GÜNLÜK. Hisse seansı ~7 saat olduğu için kriptodaki 4h
    mumun doğal karşılığı günlük bardır; üst zaman dilimi teyidi de
    HAFTALIK EMA'dan gelir (kriptoda günlük EMA'nın karşılığı).
  - Veri 15-20 dk gecikmeli olabilir (Yahoo). Kağıt modda kabul edilebilir;
    CANLI paraya geçiş için bu katman yeterli DEĞİLDİR ve o zaman gerçek
    zamanlı bir kaynakla değiştirilmelidir.
  - Ağ hatası hiçbir zaman yukarı fırlatılmaz: eldeki önbellek kullanılır,
    önbellek de yoksa sembol o tur atlanır. Kripto botunun "veri yoksa
    işlem yok" ilkesinin aynısı.

Sembol kuralı: ".IS" soneki = Borsa İstanbul (TRY), diğerleri ABD (USD).
"""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd

log = logging.getLogger("borsa.data")

GECMIS_TTL_S = 1800     # tam geçmiş (2y günlük) tazeleme aralığı
FIYAT_TTL_S = 240       # son fiyat tazeleme aralığı (5 günlük hafif çekim)


def para_birimi(symbol: str) -> str:
    """'.IS' soneki BIST demektir → TRY; kalan her şey ABD → USD."""
    return "TRY" if symbol.upper().endswith(".IS") else "USD"


class BorsaMarket:
    """Tüm semboller için önbellekli günlük veri. Tek yazıcı: borsa iş parçacığı."""

    def __init__(self, symbols: tuple[str, ...]):
        self.symbols = tuple(symbols)
        self._lock = threading.Lock()
        self._gecmis: dict[str, pd.DataFrame] = {}
        self._gecmis_ts = 0.0
        self._fiyat: dict[str, float] = {}
        self._fiyat_ts = 0.0

    # ------------------------------------------------------------- iç çekimler
    def _indir(self, period: str) -> dict[str, pd.DataFrame]:
        """Toplu indirme — tek çağrıda tüm semboller (istek nezaketi)."""
        import yfinance as yf  # tembel import: yfinance yoksa kripto botu etkilenmesin
        raw = yf.download(
            tickers=" ".join(self.symbols), period=period, interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=False,
        )
        out: dict[str, pd.DataFrame] = {}
        if raw is None or raw.empty:
            return out
        for sym in self.symbols:
            try:
                df = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue
            idx = pd.to_datetime(df.index)
            out[sym] = pd.DataFrame({
                "open_time": (idx.tz_localize("UTC") if idx.tz is None else idx)
                             .astype("int64") // 10**6,
                "open": df["Open"].to_numpy(dtype=float),
                "high": df["High"].to_numpy(dtype=float),
                "low": df["Low"].to_numpy(dtype=float),
                "close": df["Close"].to_numpy(dtype=float),
                "volume": df["Volume"].to_numpy(dtype=float),
            }).reset_index(drop=True)
        return out

    def _gecmisi_tazele(self) -> None:
        if time.time() - self._gecmis_ts < GECMIS_TTL_S and self._gecmis:
            return
        try:
            yeni = self._indir("2y")
        except Exception as e:
            log.warning("Geçmiş indirilemedi (%s) — eldeki önbellek kullanılacak", e)
            return
        if yeni:
            with self._lock:
                self._gecmis.update(yeni)   # update: kısmi başarı eldekini silmesin
                self._gecmis_ts = time.time()

    def _fiyatlari_tazele(self) -> None:
        if time.time() - self._fiyat_ts < FIYAT_TTL_S and self._fiyat:
            return
        try:
            son = self._indir("5d")
        except Exception as e:
            log.warning("Fiyatlar indirilemedi (%s) — eldeki fiyatlar kullanılacak", e)
            return
        if son:
            with self._lock:
                for sym, df in son.items():
                    self._fiyat[sym] = float(df["close"].iloc[-1])
                self._fiyat_ts = time.time()

    # ------------------------------------------------------------ genel arayüz
    def klines(self, symbol: str) -> pd.DataFrame | None:
        """2 yıllık günlük mumlar (kripto formatı). Veri yoksa None — çağıran atlar."""
        self._gecmisi_tazele()
        with self._lock:
            df = self._gecmis.get(symbol)
        return df.copy() if df is not None else None

    def last_price(self, symbol: str) -> float | None:
        self._fiyatlari_tazele()
        with self._lock:
            return self._fiyat.get(symbol)

    def haftalik_yukari(self, symbol: str, ema_period: int = 30) -> bool | None:
        """Haftalık kapanış > haftalık EMA mı? (kriptodaki günlük teyidin karşılığı)
        Veri yetersizse None — analiz aracı oy kullanmaz."""
        df = self.klines(symbol)
        if df is None or len(df) < ema_period * 5:
            return None
        s = pd.Series(
            df["close"].to_numpy(),
            index=pd.to_datetime(df["open_time"], unit="ms"),
        )
        haftalik = s.resample("W").last().dropna()
        if len(haftalik) < ema_period:
            return None
        ema = haftalik.ewm(span=ema_period, adjust=False).mean()
        return bool(haftalik.iloc[-1] > ema.iloc[-1])
