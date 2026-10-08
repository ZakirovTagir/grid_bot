"""
core/order_manager.py
Модуль взаимодействия с Bybit Demo Trading (linear perpetual).
Pleczo зафиксировано на 1x. Поддерживает LONG и SHORT.

v5:
- [PATCH 1] place_stop_market_order: добавлен triggerDirection.
  Bybit отвечает "TriggerDirection invalid (10001)", если не передать.
  Buy  (закрытие SHORT, стоп ВЫШЕ entry) → 1 (rise)
  Sell (закрытие LONG,  стоп НИЖЕ entry)  → 2 (fall)
- [PATCH 5] Добавлен close_all_positions() — рыночное закрытие всех
  открытых позиций. Используется при старте/остановке бота и для /close_all.

v4:
- Добавлено округление qty до qtyStep биржи (floor — вниз, чтобы не превысить риск).
  Bybit отвечал "Qty invalid (10001)" на 18-значный float.
- Добавлено округление цены стопа до tickSize: для LONG (Sell) — ceil,
  для SHORT (Buy) — floor. Стоп всегда ближе к entry, а не дальше.
- Кеш параметров символа через get_instruments_info.
- Fallback на дефолты BTCUSDT при ошибке API.

v3:
- set_leverage: pybit бросает исключение на retCode 110043 (leverage not modified),
  а не возвращает его в resp.
"""
from __future__ import annotations
import os
import math
import logging
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

logger = logging.getLogger(__name__)

CATEGORY = "linear"
LEVERAGE = 1

# Дефолтные фильтры для BTCUSDT linear — если API не ответил
_DEFAULT_INFO = {
    "qty_step": 0.001,
    "qty_precision": 3,
    "tick_size": 0.1,
    "tick_precision": 1,
}


class OrderManager:
    def __init__(self):
        self.session = HTTP(
            testnet=False,
            demo=True,
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
        )
        self._symbol_info_cache: dict = {}
        logger.info("OrderManager инициализирован (Bybit Demo Trading, linear, leverage=1)")

    # ------------------------------------------------------------
    #  Настройка
    # ------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int = LEVERAGE) -> bool:
        """Устанавливает плечо для символа. Для BTCUSDT обязательно перед первой сделкой."""
        try:
            resp = self.session.set_leverage(
                category=CATEGORY,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            if resp.get("retCode") == 0:
                logger.info(f"Плечо {symbol} = {leverage}x установлено")
                return True
            if resp.get("retCode") == 110043:
                logger.info(f"Плечо {symbol} уже = {leverage}x (not modified)")
                return True
            logger.error(f"Ошибка set_leverage: {resp}")
            return False
        except Exception as e:
            msg = str(e)
            if "110043" in msg or "not modified" in msg:
                logger.info(f"Плечо {symbol} уже = {leverage}x (not modified)")
                return True
            logger.error(f"Исключение set_leverage: {e}")
            return False

    # ------------------------------------------------------------
    #  Параметры символа (qtyStep, tickSize)
    # ------------------------------------------------------------
    @staticmethod
    def _precision_from_str(s: str) -> int:
        """'0.001' -> 3, '0.1' -> 1, '1' -> 0."""
        if "." not in s:
            return 0
        return len(s.split(".")[1])

    def _get_symbol_info(self, symbol: str) -> dict:
        """Кеширует qty_step/tick_size по символу."""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        info = dict(_DEFAULT_INFO)
        try:
            resp = self.session.get_instruments_info(
                category=CATEGORY, symbol=symbol
            )
            if resp.get("retCode") == 0 and resp.get("result", {}).get("list"):
                item = resp["result"]["list"][0]
                lot = item.get("lotSizeFilter", {}) or {}
                pf = item.get("priceFilter", {}) or {}

                qty_step_str = str(lot.get("qtyStep", "0.001"))
                info["qty_step"] = float(qty_step_str)
                info["qty_precision"] = self._precision_from_str(qty_step_str)

                tick_str = str(pf.get("tickSize", "0.1"))
                info["tick_size"] = float(tick_str)
                info["tick_precision"] = self._precision_from_str(tick_str)

                logger.info(
                    f"{symbol}: qtyStep={info['qty_step']} "
                    f"tickSize={info['tick_size']}"
                )
            else:
                logger.warning(
                    f"get_instruments_info {symbol} пусто, "
                    f"использую defaults {_DEFAULT_INFO}"
                )
        except Exception as e:
            logger.error(
                f"Исключение get_instruments_info {symbol}: {e}, "
                f"использую defaults"
            )

        self._symbol_info_cache[symbol] = info
        return info

    def round_qty(self, symbol: str, qty: float) -> float:
        """
        Округляет qty ВНИЗ до qtyStep биржи.
        Всегда floor — фактический риск не превысит MAX_LOSS_PER_TRADE.
        """
        info = self._get_symbol_info(symbol)
        step = info["qty_step"]
        precision = info["qty_precision"]
        if step <= 0:
            return qty
        floored = math.floor(qty / step) * step
        return round(floored, precision)

    def round_stop_price(self, symbol: str, price: float, side: str) -> float:
        """
        Округляет цену стопа до tickSize.
        side — сторона ордера ('Sell' для LONG, 'Buy' для SHORT).
        Стоп округляется В СТОРОНУ entry (короче дистанция, меньше риск):
        - 'Sell' (LONG, стоп ниже entry) → ceil
        - 'Buy'  (SHORT, стоп выше entry) → floor
        """
        info = self._get_symbol_info(symbol)
        tick = info["tick_size"]
        precision = info["tick_precision"]
        if tick <= 0:
            return price
        if side == "Sell":
            rounded = math.ceil(price / tick) * tick
        else:
            rounded = math.floor(price / tick) * tick
        return round(rounded, precision)

    # ------------------------------------------------------------
    #  Ордера
    # ------------------------------------------------------------
    def place_market_order(self, symbol: str, side: str, qty: float,
                           reduce_only: bool = False) -> str | None:
        """
        Рыночный ордер.
        side: 'Buy' (открыть LONG или закрыть SHORT), 'Sell' (открыть SHORT или закрыть LONG)
        reduce_only: True для закрытия позиции
        qty автоматически округляется до qtyStep.
        """
        raw_qty = qty
        qty = self.round_qty(symbol, qty)
        if qty <= 0:
            logger.error(
                f"qty={raw_qty} округлилось до 0, ордер не выставлен"
            )
            return None
        if abs(qty - raw_qty) > 1e-12:
            logger.info(f"qty округлено: {raw_qty:.10f} → {qty}")
        try:
            kwargs = dict(
                category=CATEGORY,
                symbol=symbol,
                side=side,
                orderType="Market",
                qty=str(qty),
                positionIdx=0,
            )
            if reduce_only:
                kwargs["reduceOnly"] = True
            resp = self.session.place_order(**kwargs)
            if resp.get("retCode") == 0:
                order_id = resp["result"]["orderId"]
                logger.info(
                    f"Market {side} {symbol} {qty} "
                    f"(reduceOnly={reduce_only}) → {order_id}"
                )
                return order_id
            logger.error(f"Ошибка market order: {resp}")
            return None
        except Exception as e:
            logger.error(f"Исключение market order: {e}")
            return None

    def place_limit_order(self, symbol: str, side: str, qty: float,
                          price: float, reduce_only: bool = False) -> str | None:
        """Лимитный ордер. qty округляется до qtyStep."""
        raw_qty = qty
        qty = self.round_qty(symbol, qty)
        if qty <= 0:
            logger.error(
                f"qty={raw_qty} округлилось до 0, ордер не выставлен"
            )
            return None
        try:
            kwargs = dict(
                category=CATEGORY,
                symbol=symbol,
                side=side,
                orderType="Limit",
                qty=str(qty),
                price=str(price),
                timeInForce="GTC",
                positionIdx=0,
            )
            if reduce_only:
                kwargs["reduceOnly"] = True
            resp = self.session.place_order(**kwargs)
            if resp.get("retCode") == 0:
                order_id = resp["result"]["orderId"]
                logger.info(f"Limit {side} {symbol} {qty} @ {price} → {order_id}")
                return order_id
            logger.error(f"Ошибка limit order: {resp}")
            return None
        except Exception as e:
            logger.error(f"Исключение limit order: {e}")
            return None

    def place_stop_market_order(self, symbol: str, side: str, qty: float,
                                stop_price: float) -> str | None:
        """
        Стоп-маркет (reduceOnly) для защиты открытой позиции.
        side: 'Sell' для LONG-позиции, 'Buy' для SHORT-позиции.
        qty округляется до qtyStep, цена стопа — до tickSize (в сторону entry).
        """
        raw_qty = qty
        qty = self.round_qty(symbol, qty)
        if qty <= 0:
            logger.error(
                f"qty={raw_qty} округлилось до 0, стоп не выставлен"
            )
            return None

        raw_price = stop_price
        stop_price = self.round_stop_price(symbol, stop_price, side)
        if abs(stop_price - raw_price) > 1e-12:
            logger.info(f"stop price округлена: {raw_price:.6f} → {stop_price}")

        # [PATCH 1] triggerDirection — обязателен для условных ордеров
        # на Bybit linear. Без него биржа отвечает "TriggerDirection invalid (10001)".
        #   Buy  (закрытие SHORT, стоп ВЫШЕ entry) → 1 (rise)
        #   Sell (закрытие LONG,  стоп НИЖЕ entry)  → 2 (fall)
        trigger_direction = 1 if side == "Buy" else 2

        try:
            resp = self.session.place_order(
                category=CATEGORY,
                symbol=symbol,
                side=side,
                orderType="Market",
                qty=str(qty),
                triggerPrice=str(stop_price),
                triggerBy="LastPrice",
                triggerDirection=trigger_direction,   # [PATCH 1]
                timeInForce="IOC",
                reduceOnly=True,
                positionIdx=0,
            )
            if resp.get("retCode") == 0:
                order_id = resp["result"]["orderId"]
                logger.info(
                    f"StopMarket {side} {symbol} {qty} @ {stop_price} "
                    f"(triggerDirection={trigger_direction}) → {order_id}"
                )
                return order_id
            logger.error(f"Ошибка stop-market: {resp}")
            return None
        except Exception as e:
            logger.error(f"Исключение stop-market: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            resp = self.session.cancel_order(
                category=CATEGORY, symbol=symbol, orderId=order_id,
            )
            if resp.get("retCode") == 0:
                logger.info(f"Ордер {order_id} отменён")
                return True
            logger.error(f"Не удалось отменить {order_id}: {resp}")
            return False
        except Exception as e:
            logger.error(f"Исключение cancel_order: {e}")
            return False

    def cancel_all_orders(self, symbol: str) -> bool:
        """Отменяет все открытые ордера по символу."""
        try:
            resp = self.session.cancel_all_orders(
                category=CATEGORY, symbol=symbol,
            )
            if resp.get("retCode") == 0:
                logger.info(f"Все ордера {symbol} отменены")
                return True
            logger.error(f"Ошибка cancel_all_orders: {resp}")
            return False
        except Exception as e:
            logger.error(f"Исключение cancel_all_orders: {e}")
            return False

    # ------------------------------------------------------------
    #  Позиция
    # ------------------------------------------------------------
    def get_position(self, symbol: str) -> dict | None:
        """
        Возвращает текущую позицию по символу или None.
        {'side': 'LONG'|'SHORT', 'qty': float, 'entry_price': float,
         'unrealized_pnl': float, 'stop_loss': float}
        """
        try:
            resp = self.session.get_positions(category=CATEGORY, symbol=symbol)
            if resp.get("retCode") != 0:
                logger.error(f"Ошибка get_positions: {resp}")
                return None
            positions = resp["result"]["list"]
            for p in positions:
                size = float(p.get("size", 0))
                if size > 0:
                    return {
                        "symbol": symbol,
                        "side": "LONG" if p["side"] == "Buy" else "SHORT",
                        "qty": size,
                        "entry_price": float(p.get("avgPrice", 0)),
                        "unrealized_pnl": float(p.get("unrealisedPnl", 0)),
                        "stop_loss": float(p.get("stopLoss", 0) or 0),
                    }
            return None
        except Exception as e:
            logger.error(f"Исключение get_position: {e}")
            return None

    def close_position(self, symbol: str, qty: float | None = None) -> str | None:
        """
        Закрывает позицию рыночным ордером reduceOnly.
        Если qty не указан — берёт размер из текущей позиции.
        """
        pos = self.get_position(symbol)
        if pos is None:
            logger.warning(f"Нет открытой позиции {symbol} для закрытия")
            return None
        close_qty = qty if qty is not None else pos["qty"]
        close_side = "Sell" if pos["side"] == "LONG" else "Buy"
        return self.place_market_order(symbol, close_side, close_qty, reduce_only=True)

    # [PATCH 5] Закрытие всех открытых позиций (все символы).
    # Используется при старте/остановке бота и для /close_all.
    def close_all_positions(self) -> list:
        """
        Закрывает все открытые позиции на Demo Trading по рынку.
        Перед закрытием каждой позиции отменяет все её ордера
        (в т.ч. висящие стопы), чтобы не осталось «призраков».
        Возвращает список dict с результатами.
        """
        results: list = []
        try:
            resp = self.session.get_positions(
                category=CATEGORY, settleCoin="USDT"
            )
            if resp.get("retCode") != 0:
                logger.error(f"close_all_positions: get_positions failed: {resp}")
                return results
            positions = resp["result"]["list"]
            for p in positions:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                symbol = p["symbol"]
                pos_side = p["side"]  # 'Buy' или 'Sell'
                close_side = "Sell" if pos_side == "Buy" else "Buy"

                # Отменяем висящие ордера, включая стопы
                try:
                    self.cancel_all_orders(symbol)
                except Exception as e:
                    logger.warning(f"cancel_all_orders {symbol} failed: {e}")

                logger.info(
                    f"close_all_positions: закрываю {symbol} "
                    f"{pos_side} size={size}"
                )
                order_id = self.place_market_order(
                    symbol, close_side, size, reduce_only=True
                )
                results.append({
                    "symbol": symbol,
                    "side": pos_side,
                    "size": size,
                    "order_id": order_id,
                    "success": order_id is not None,
                })
            if not results:
                logger.info("close_all_positions: открытых позиций нет")
        except Exception as e:
            logger.error(f"close_all_positions exception: {e}")
        return results

    # ------------------------------------------------------------
    #  Вспомогательные
    # ------------------------------------------------------------
    def get_open_orders(self, symbol: str) -> list:
        try:
            resp = self.session.get_open_orders(category=CATEGORY, symbol=symbol)
            if resp.get("retCode") == 0:
                return resp["result"]["list"]
            logger.error(f"Ошибка get_open_orders: {resp}")
            return []
        except Exception as e:
            logger.error(f"Исключение get_open_orders: {e}")
            return []

    def get_wallet_usdt(self) -> float | None:
        """Баланс USDT на unified-аккаунте."""
        try:
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            if resp.get("retCode") != 0:
                return None
            for coin in resp["result"]["list"][0]["coin"]:
                if coin["coin"] == "USDT":
                    return float(coin["walletBalance"])
            return None
        except Exception as e:
            logger.error(f"Ошибка get_wallet_usdt: {e}")
            return None

    def get_last_price(self, symbol: str) -> float | None:
        try:
            resp = self.session.get_tickers(category=CATEGORY, symbol=symbol)
            if resp.get("retCode") == 0:
                return float(resp["result"]["list"][0]["lastPrice"])
            return None
        except Exception as e:
            logger.error(f"Ошибка get_last_price: {e}")
            return None

    def get_candles(self, symbol: str, interval: str = "15", limit: int = 100) -> list:
        """Последние свечи (для main.py). Возвращает список как есть."""
        try:
            resp = self.session.get_kline(
                category=CATEGORY, symbol=symbol, interval=interval, limit=limit,
            )
            if resp.get("retCode") == 0:
                return resp["result"]["list"]
            logger.error(f"Ошибка get_candles: {resp}")
            return []
        except Exception as e:
            logger.error(f"Исключение get_candles: {e}")
            return []