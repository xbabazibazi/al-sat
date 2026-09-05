"""Borsa geçidi (gateway).

İki kritik tasarım kararı:

1. PİYASA VERİSİ HER ZAMAN GERÇEK BORSADAN gelir (api.binance.com, herkese
   açık uçlar — anahtar gerekmez). Testnet'in sığ/yapay fiyat verisiyle
   sinyal üretmek yanıltıcıdır; testnet yalnızca EMİR göndermek için kullanılır.

2. Stop emri BORSA TARAFINDA durur (STOP_LOSS_LIMIT). Bot çökse, internet
   kesilse bile açık pozisyon korumasız kalmaz.

Üç mod:
  dry_run : anahtar gerekmez; emirler yerel olarak simüle edilir.
  testnet : emirler testnet.binance.vision'a gider.
  live    : emirler gerçek hesaba gider.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import requests

from .config import Config

log = logging.getLogger("exchange")

PROD_API = "https://api.binance.com"

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
]


@dataclass(frozen=True)
class SymbolFilters:
    step_size: float       # LOT_SIZE stepSize  (miktar adımı)
    min_qty: float         # LOT_SIZE minQty
    tick_size: float       # PRICE_FILTER tickSize (fiyat adımı)
    min_notional: float    # NOTIONAL/MIN_NOTIONAL (minimum işlem tutarı, USDT)


@dataclass(frozen=True)
class Fill:
    avg_price: float
    executed_qty: float


def floor_to_step(value: float, step: float) -> float:
    """Borsa adımına AŞAĞI yuvarlama. round() kullanmak yukarı yuvarlayıp
    'insufficient balance' / 'LOT_SIZE' hatası üretebilir."""
    if step <= 0:
        return value
    return math.floor(value / step + 1e-12) * step


class MarketData:
    """Gerçek borsadan (prod) herkese açık veri. Tüm modlar bunu kullanır."""

    def __init__(self):
        self._session = requests.Session()
        self._filters_cache: dict[str, SymbolFilters] = {}

    def klines(self, symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
        resp = self._session.get(
            f"{PROD_API}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        df = pd.DataFrame(resp.json(), columns=KLINE_COLUMNS)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df["open_time"] = df["open_time"].astype("int64")
        df["close_time"] = df["close_time"].astype("int64")
        return df

    def last_price(self, symbol: str) -> float:
        resp = self._session.get(
            f"{PROD_API}/api/v3/ticker/price", params={"symbol": symbol}, timeout=10
        )
        resp.raise_for_status()
        return float(resp.json()["price"])

    def filters(self, symbol: str) -> SymbolFilters:
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        resp = self._session.get(
            f"{PROD_API}/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=10
        )
        resp.raise_for_status()
        info = resp.json()["symbols"][0]
        f = _parse_filters(info)
        self._filters_cache[symbol] = f
        return f


def _parse_filters(symbol_info: dict) -> SymbolFilters:
    step = min_qty = tick = 0.0
    min_notional = 5.0
    for flt in symbol_info["filters"]:
        t = flt["filterType"]
        if t == "LOT_SIZE":
            step = float(flt["stepSize"])
            min_qty = float(flt["minQty"])
        elif t == "PRICE_FILTER":
            tick = float(flt["tickSize"])
        elif t in ("NOTIONAL", "MIN_NOTIONAL"):
            min_notional = float(flt.get("minNotional", flt.get("notional", 5.0)))
    return SymbolFilters(step, min_qty, tick, min_notional)


# --------------------------------------------------------------------- broker
class Broker:
    """Emir gönderen taraf. testnet/live için python-binance, dry_run için simülasyon."""

    def free_balance(self, asset: str) -> float: ...
    def market_buy(self, symbol: str, qty: float) -> Optional[Fill]: ...
    def market_sell(self, symbol: str, qty: float) -> Optional[Fill]: ...
    def limit_buy(self, symbol: str, qty: float, price: float) -> Optional[int]: ...
    def place_stop(self, symbol: str, qty: float, stop_price: float, limit_price: float) -> Optional[int]: ...
    def cancel_order(self, symbol: str, order_id: int) -> bool: ...
    def order_status(self, symbol: str, order_id: int) -> tuple[str, Optional[Fill]]: ...
    def open_orders(self, symbol: str) -> list[dict]: ...
    def base_balance(self, symbol: str) -> float: ...


class BinanceBroker(Broker):
    def __init__(self, cfg: Config):
        from binance.client import Client  # geç import: dry_run modunda gerek yok

        if cfg.mode == "live":
            self.client = Client(cfg.live_key, cfg.live_secret)
        else:
            self.client = Client(cfg.testnet_key, cfg.testnet_secret, testnet=True)
        self._filters_cache: dict[str, SymbolFilters] = {}

    def venue_filters(self, symbol: str) -> SymbolFilters:
        """Emirler bu borsaya gittiği için filtreler de BU borsadan alınır."""
        if symbol not in self._filters_cache:
            info = self.client.get_symbol_info(symbol)
            self._filters_cache[symbol] = _parse_filters(info)
        return self._filters_cache[symbol]

    def free_balance(self, asset: str) -> float:
        bal = self.client.get_asset_balance(asset=asset)
        return float(bal["free"]) if bal else 0.0

    def base_balance(self, symbol: str) -> float:
        base = symbol.replace("USDT", "")
        return self.free_balance(base)

    @staticmethod
    def _fill_from_order(order: dict) -> Fill:
        executed = float(order.get("executedQty", 0) or 0)
        quote = float(order.get("cummulativeQuoteQty", 0) or 0)
        avg = quote / executed if executed else 0.0
        return Fill(avg_price=avg, executed_qty=executed)

    def market_buy(self, symbol: str, qty: float) -> Optional[Fill]:
        try:
            order = self.client.order_market_buy(symbol=symbol, quantity=qty)
            return self._fill_from_order(order)
        except Exception as e:
            log.error("[%s] MARKET BUY hatası: %s", symbol, e)
            return None

    def market_sell(self, symbol: str, qty: float) -> Optional[Fill]:
        try:
            order = self.client.order_market_sell(symbol=symbol, quantity=qty)
            return self._fill_from_order(order)
        except Exception as e:
            log.error("[%s] MARKET SELL hatası: %s", symbol, e)
            return None

    def limit_buy(self, symbol: str, qty: float, price: float) -> Optional[int]:
        """Maker komisyonu için limit alım (GTC). Dolmayan emir trader
        tarafında timeout sonrası iptal edilip market emrine dönülür."""
        try:
            order = self.client.order_limit_buy(
                symbol=symbol, quantity=qty,
                price=f"{price:.8f}".rstrip("0").rstrip("."),
            )
            return int(order["orderId"])
        except Exception as e:
            log.error("[%s] LIMIT BUY hatası (fiyat=%s): %s", symbol, price, e)
            return None

    def place_stop(self, symbol: str, qty: float, stop_price: float, limit_price: float) -> Optional[int]:
        try:
            order = self.client.create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                timeInForce="GTC",
                quantity=qty,
                stopPrice=f"{stop_price:.8f}".rstrip("0").rstrip("."),
                price=f"{limit_price:.8f}".rstrip("0").rstrip("."),
            )
            return int(order["orderId"])
        except Exception as e:
            log.error("[%s] STOP emri hatası (stop=%s): %s", symbol, stop_price, e)
            return None

    def cancel_order(self, symbol: str, order_id: int) -> bool:
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
            return True
        except Exception as e:
            log.warning("[%s] Emir iptali başarısız (id=%s): %s", symbol, order_id, e)
            return False

    def order_status(self, symbol: str, order_id: int) -> tuple[str, Optional[Fill]]:
        try:
            order = self.client.get_order(symbol=symbol, orderId=order_id)
            status = order["status"]
            # Kısmi dolumda da fill bilgisini döndür (iptal sonrası eldeki miktar bilinsin)
            fill = self._fill_from_order(order)
            if fill.executed_qty <= 0:
                fill = None
            return status, fill
        except Exception as e:
            log.error("[%s] Emir sorgu hatası (id=%s): %s", symbol, order_id, e)
            return "UNKNOWN", None

    def open_orders(self, symbol: str) -> list[dict]:
        try:
            return self.client.get_open_orders(symbol=symbol)
        except Exception as e:
            log.error("[%s] Açık emir sorgusu hatası: %s", symbol, e)
            return []


class DryRunBroker(Broker):
    """Anahtar gerektirmeyen simülasyon: gerçek fiyat, sanal bakiye.

    Sanal stop emirleri her poll'da gerçek fiyatla karşılaştırılır; borsa
    tarafı stop davranışına yakınsar (30 sn çözünürlükle).
    """

    FEE = 0.001          # %0.1 taker komisyonu
    MAKER_FEE = 0.00075  # limit dolumları için maker komisyonu
    SLIPPAGE = 0.0005    # %0.05 kayma

    def __init__(self, market: MarketData, state):
        self.market = market
        self.state = state
        self._next_order_id = int(time.time())
        if not self.state.get_kv("dry_usdt"):
            self.state.set_kv("dry_usdt", "10000.0")

    # sanal cüzdan -----------------------------------------------------------
    def _get_usdt(self) -> float:
        return float(self.state.get_kv("dry_usdt", "10000.0"))

    def _set_usdt(self, v: float) -> None:
        self.state.set_kv("dry_usdt", f"{v:.8f}")

    def _get_base(self, symbol: str) -> float:
        return float(self.state.get_kv(f"dry_base_{symbol}", "0"))

    def _set_base(self, symbol: str, v: float) -> None:
        self.state.set_kv(f"dry_base_{symbol}", f"{v:.8f}")

    def free_balance(self, asset: str) -> float:
        if asset == "USDT":
            return self._get_usdt()
        return float(self.state.get_kv(f"dry_base_{asset}USDT", "0"))

    def base_balance(self, symbol: str) -> float:
        return self._get_base(symbol)

    # emirler ----------------------------------------------------------------
    def market_buy(self, symbol: str, qty: float) -> Optional[Fill]:
        price = self.market.last_price(symbol) * (1 + self.SLIPPAGE)
        cost = price * qty
        fee = cost * self.FEE
        if cost + fee > self._get_usdt():
            log.error("[%s] DRY-RUN: yetersiz sanal bakiye", symbol)
            return None
        self._set_usdt(self._get_usdt() - cost - fee)
        self._set_base(symbol, self._get_base(symbol) + qty)
        return Fill(avg_price=price, executed_qty=qty)

    def market_sell(self, symbol: str, qty: float) -> Optional[Fill]:
        price = self.market.last_price(symbol) * (1 - self.SLIPPAGE)
        qty = min(qty, self._get_base(symbol))
        if qty <= 0:
            return None
        proceeds = price * qty
        self._set_usdt(self._get_usdt() + proceeds * (1 - self.FEE))
        self._set_base(symbol, self._get_base(symbol) - qty)
        return Fill(avg_price=price, executed_qty=qty)

    def limit_buy(self, symbol: str, qty: float, price: float) -> Optional[int]:
        self._next_order_id += 1
        oid = self._next_order_id
        last = self.market.last_price(symbol)
        if last <= price:
            # anında maker dolumu simülasyonu
            cost = price * qty
            fee = cost * self.MAKER_FEE
            if cost + fee > self._get_usdt():
                log.error("[%s] DRY-RUN: limit alım için yetersiz sanal bakiye", symbol)
                return None
            self._set_usdt(self._get_usdt() - cost - fee)
            self._set_base(symbol, self._get_base(symbol) + qty)
            self.state.set_kv(f"dry_limit_{symbol}_{oid}", f"{price}|{qty}|FILLED")
        else:
            self.state.set_kv(f"dry_limit_{symbol}_{oid}", f"{price}|{qty}|OPEN")
        return oid

    def place_stop(self, symbol: str, qty: float, stop_price: float, limit_price: float) -> Optional[int]:
        self._next_order_id += 1
        oid = self._next_order_id
        self.state.set_kv(f"dry_stop_{symbol}_{oid}", f"{stop_price}|{qty}|OPEN")
        return oid

    def cancel_order(self, symbol: str, order_id: int) -> bool:
        for prefix in ("dry_stop", "dry_limit"):
            key = f"{prefix}_{symbol}_{order_id}"
            raw = self.state.get_kv(key)
            if raw and raw.endswith("OPEN"):
                price, qty, _ = raw.split("|")
                self.state.set_kv(key, f"{price}|{qty}|CANCELED")
                return True
        return False

    def order_status(self, symbol: str, order_id: int) -> tuple[str, Optional[Fill]]:
        # Limit alım emri mi?
        lkey = f"dry_limit_{symbol}_{order_id}"
        raw = self.state.get_kv(lkey)
        if raw:
            price_s, qty_s, status = raw.split("|")
            if status == "FILLED":
                return "FILLED", Fill(avg_price=float(price_s), executed_qty=float(qty_s))
            if status == "OPEN" and self.market.last_price(symbol) <= float(price_s):
                # limit seviyesine gelindi — maker dolumu simüle et
                price, qty = float(price_s), float(qty_s)
                cost = price * qty
                self._set_usdt(self._get_usdt() - cost * (1 + self.MAKER_FEE))
                self._set_base(symbol, self._get_base(symbol) + qty)
                self.state.set_kv(lkey, f"{price_s}|{qty_s}|FILLED")
                return "FILLED", Fill(avg_price=price, executed_qty=qty)
            return ("NEW", None) if status == "OPEN" else (status, None)

        # Stop satış emri mi?
        key = f"dry_stop_{symbol}_{order_id}"
        raw = self.state.get_kv(key)
        if not raw:
            return "UNKNOWN", None
        stop_s, qty_s, status = raw.split("|")
        stop, qty = float(stop_s), float(qty_s)
        if status != "OPEN":
            return status, None
        price = self.market.last_price(symbol)
        if price <= stop:
            # stop tetiklendi — stop fiyatından (küçük kaymayla) satış simüle et
            fill_price = min(price, stop) * (1 - self.SLIPPAGE)
            proceeds = fill_price * qty
            self._set_usdt(self._get_usdt() + proceeds * (1 - self.FEE))
            self._set_base(symbol, max(0.0, self._get_base(symbol) - qty))
            self.state.set_kv(key, f"{stop_s}|{qty_s}|FILLED")
            return "FILLED", Fill(avg_price=fill_price, executed_qty=qty)
        return "NEW", None

    def open_orders(self, symbol: str) -> list[dict]:
        return []  # mutabakat gerçek borsa modlarında anlamlı


def build_broker(cfg: Config, market: MarketData, state) -> Broker:
    if cfg.mode == "dry_run":
        return DryRunBroker(market, state)
    return BinanceBroker(cfg)
