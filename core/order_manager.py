"""
core/order_manager.py
Модуль взаимодействия с Bybit Testnet API.
Поддерживает лимитные и стоп-маркет ордера, проверку статуса позиций.
"""
from __future__ import annotations
import os
import logging
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

logger = logging.getLogger(__name__)

class OrderManager:
    def __init__(self):
        self.session = HTTP(
            testnet=True,
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
        )
        logger.info("OrderManager инициализирован (Bybit Testnet)")

    def place_limit_order(self, symbol: str, side: str, qty: float, price: float) -> str | None:
        """
        Выставляет лимитный ордер.
        side: 'Buy' (LONG) или 'Sell' (SHORT)
        Возвращает orderId или None при ошибке.
        """
        try:
            resp = self.session.place_order(
                category="spot",
                symbol=symbol,
                side=side,
                orderType="Limit",
                qty=str(qty),
                price=str(price),
                timeInForce="GTC",
            )
            if resp.get("retCode") == 0:
                order_id = resp["result"]["orderId"]
                logger.info(f"Лимитный ордер {order_id}: {symbol} {side} {qty} @ {price}")
                return order_id
            else:
                logger.error(f"Ошибка лимитного ордера: {resp}")
                return None
        except Exception as e:
            logger.error(f"Исключение при лимитном ордере: {e}")
            return None

    def place_stop_market_order(self, symbol: str, side: str, qty: float, stop_price: float) -> str | None:
        """
        Выставляет стоп-маркет ордер (для стоп-лосса / безубытка).
        side: 'Buy' (для шорта) или 'Sell' (для лонга)
        """
        try:
            resp = self.session.place_order(
                category="spot",
                symbol=symbol,
                side=side,
                orderType="Market",
                qty=str(qty),
                triggerPrice=str(stop_price),
                triggerBy="LastPrice",
                timeInForce="IOC",
            )
            if resp.get("retCode") == 0:
                order_id = resp["result"]["orderId"]
                logger.info(f"Стоп-маркет ордер {order_id}: {symbol} {side} {qty} @ stop {stop_price}")
                return order_id
            else:
                logger.error(f"Ошибка стоп-маркет ордера: {resp}")
                return None
        except Exception as e:
            logger.error(f"Исключение при стоп-маркет ордере: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Отменяет ордер по ID."""
        try:
            resp = self.session.cancel_order(
                category="spot",
                symbol=symbol,
                orderId=order_id,
            )
            if resp.get("retCode") == 0:
                logger.info(f"Ордер {order_id} отменён")
                return True
            else:
                logger.error(f"Не удалось отменить ордер {order_id}: {resp}")
                return False
        except Exception as e:
            logger.error(f"Исключение при отмене ордера: {e}")
            return False

    def get_open_orders(self, symbol: str) -> list:
        """Возвращает список открытых ордеров по символу (list of dict)."""
        try:
            resp = self.session.get_open_orders(
                category="spot",
                symbol=symbol,
            )
            if resp.get("retCode") == 0:
                return resp["result"]["list"]
            else:
                logger.error(f"Ошибка получения открытых ордеров: {resp}")
                return []
        except Exception as e:
            logger.error(f"Исключение получения открытых ордеров: {e}")
            return []

    def get_position(self, symbol: str) -> dict | None:
        """
        Возвращает информацию о текущей позиции на споте.
        Для spot-торговли позицией считается суммарный баланс актива.
        """
        try:
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            if resp.get("retCode") != 0:
                return None
            for asset in resp["result"]["list"][0]["coin"]:
                if asset["coin"] == symbol.replace("USDT", ""):
                    return {
                        "symbol": symbol,
                        "qty": float(asset["walletBalance"]),
                        "side": "Long" if float(asset["walletBalance"]) > 0 else "Short",
                    }
            return None
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return None

    def get_last_price(self, symbol: str) -> float | None:
        """Возвращает последнюю цену по инструменту."""
        try:
            resp = self.session.get_tickers(category="spot", symbol=symbol)
            if resp.get("retCode") == 0:
                return float(resp["result"]["list"][0]["lastPrice"])
            else:
                return None
        except Exception as e:
            logger.error(f"Ошибка получения цены: {e}")
            return None