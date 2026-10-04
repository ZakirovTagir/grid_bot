"""
core/order_manager.py
Модуль взаимодействия с Bybit Testnet API (linear perpetual).
Pleczo зафиксировано на 1x. Поддерживает LONG и SHORT.

Логика:
- Открытие позиции: place_market_order или place_limit_order
- Стоп-лосс: place_stop_market_order (reduceOnly)
- Закрытие: close_position (reduceOnly market)
- Получение позиции: get_position (категория linear)
- Установка плеча: set_leverage (один раз при старте)
"""
from __future__ import annotations
import os
import logging
import time
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

logger = logging.getLogger(__name__)

CATEGORY = "linear"
LEVERAGE = 1


class OrderManager:
    def __init__(self):
        self.session = HTTP(
            testnet=False,
            demo=True,
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
        )
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
            # Уже стоит такое же плечо — не ошибка
            if resp.get("retCode") == 110043:
                logger.info(f"Плечо {symbol} уже = {leverage}x")
                return True
            logger.error(f"Ошибка set_leverage: {resp}")
            return False
        except Exception as e:
            logger.error(f"Исключение set_leverage: {e}")
            return False

    # ------------------------------------------------------------
    #  Ордера
    # ------------------------------------------------------------
    def place_market_order(self, symbol: str, side: str, qty: float,
                           reduce_only: bool = False) -> str | None:
        """
        Рыночный ордер.
        side: 'Buy' (открыть LONG или закрыть SHORT), 'Sell' (открыть SHORT или закрыть LONG)
        reduce_only: True для закрытия позиции
        """
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
        """Лимитный ордер."""
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
        side: 'Sell' для LONG-позиции, 'Buy' для SHORT-позиции
        """
        try:
            resp = self.session.place_order(
                category=CATEGORY,
                symbol=symbol,
                side=side,
                orderType="Market",
                qty=str(qty),
                triggerPrice=str(stop_price),
                triggerBy="LastPrice",
                timeInForce="IOC",
                reduceOnly=True,
                positionIdx=0,
            )
            if resp.get("retCode") == 0:
                order_id = resp["result"]["orderId"]
                logger.info(f"StopMarket {side} {symbol} {qty} @ {stop_price} → {order_id}")
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