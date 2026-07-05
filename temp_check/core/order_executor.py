"""
core/order_executor.py
Обертка над pybit для отправки ордеров и получения данных.
"""
import time
from typing import Dict, Optional, List
from pybit.unified_trading import HTTP
from config.settings import API_KEY, API_SECRET, TESTNET, LEVERAGE, MARGIN_MODE
from config.pairs import PAIRS_CONFIG
import logging

logger = logging.getLogger(__name__)


class BybitExecutor:
    def __init__(self):
        self.session = HTTP(
            testnet=TESTNET,
            api_key=API_KEY,
            api_secret=API_SECRET,
            recv_window=15000,
            timeout=30
        )
        self.symbol_info_cache = {}

    def _set_leverage(self, symbol: str):
        """Установить плечо 1x и изолированную маржу."""
        try:
            self.session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(LEVERAGE),
                sellLeverage=str(LEVERAGE),
            )
        except Exception as e:
            logger.warning(f"Не удалось установить плечо для {symbol}: {e}")

    def _ensure_symbol_info(self, symbol: str):
        """Кеширование минимальных параметров торговли."""
        if symbol not in self.symbol_info_cache:
            info = PAIRS_CONFIG.get(symbol, {})
            self.symbol_info_cache[symbol] = info
        return self.symbol_info_cache[symbol]
    def get_usdt_balance(self) -> float:
        """Возвращает доступный баланс USDT в едином торговом счёте."""
        try:
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            # Временный отладочный вывод структуры ответа
            logger.debug(f"Ответ API баланса: {resp}")

            if resp.get('retCode') != 0:
                logger.error(f"Ошибка API баланса: {resp.get('retMsg')}")
                return 0.0

            # Перебираем монеты в ответе
            for asset in resp['result']['list'][0]['coin']:
                if asset['coin'] == 'USDT':
                    # Пробуем несколько вариантов названий доступного баланса
                    balance_str = (
                        asset.get('availableToWithdraw') or
                        asset.get('equity') or
                        asset.get('walletBalance') or
                        '0'
                    )
                    try:
                        return float(balance_str)
                    except (ValueError, TypeError):
                        logger.error(f"Не удалось преобразовать баланс: {balance_str}")
                        return 0.0
            return 0.0
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return 0.0

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Последняя цена пары."""
        try:
            resp = self.session.get_tickers(category="linear", symbol=symbol)
            return float(resp['result']['list'][0]['lastPrice'])
        except Exception as e:
            logger.error(f"Ошибка получения цены {symbol}: {e}")
            return None

    def place_limit_orders(self, symbol: str, orders: List[Dict]) -> bool:
        """
        Размещает лимитные ордера.
        orders = [{'side': 'Buy'/'Sell', 'price': float, 'qty': float}, ...]
        Возвращает True, если все размещены успешно.
        """
        info = self._ensure_symbol_info(symbol)
        price_step = info.get('price_step', 0.01)
        min_qty = info.get('min_qty', 0.001)

        success = True
        for order in orders:
            try:
                price = round(order['price'] / price_step) * price_step
                qty = max(min_qty, round(order['qty'] / min_qty) * min_qty)
                self.session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=order['side'],
                    orderType="Limit",
                    qty=str(qty),
                    price=str(price),
                    timeInForce="GTC",
                    positionIdx=0,  # 0 для одностороннего режима
                )
                logger.info(f"Размещён {order['side']} лимитник: {qty}@{price} ({symbol})")
            except Exception as e:
                logger.error(f"Ошибка размещения ордера {symbol}: {e}")
                success = False
        return success

    def cancel_all_orders(self, symbol: str):
        """Отменяет все активные ордера по символу."""
        try:
            self.session.cancel_all_orders(category="linear", symbol=symbol)
            logger.info(f"Отменены все ордера по {symbol}")
        except Exception as e:
            logger.error(f"Ошибка отмены ордеров {symbol}: {e}")

    def get_positions(self, symbol: str) -> Dict:
        """Возвращает текущую позицию (размер, сторона, PnL)."""
        try:
            resp = self.session.get_positions(category="linear", symbol=symbol)
            for pos in resp['result']['list']:
                if float(pos['size']) != 0:
                    return {
                        'size': float(pos['size']),
                        'side': pos['side'],
                        'unrealisedPnl': float(pos['unrealisedPnl']),
                    }
        except Exception as e:
            logger.error(f"Ошибка получения позиций {symbol}: {e}")
        return {}

    def close_position_market(self, symbol: str) -> bool:
        """Закрыть текущую позицию по рынку."""
        pos = self.get_positions(symbol)
        if not pos or pos['size'] == 0:
            return True

        # Определяем обратную сторону
        close_side = "Sell" if pos['side'] == "Buy" else "Buy"
        try:
            info = self._ensure_symbol_info(symbol)
            qty = max(info.get('min_qty', 0.001), pos['size'])
            self.session.place_order(
                category="linear",
                symbol=symbol,
                side=close_side,
                orderType="Market",
                qty=str(qty),
                timeInForce="IOC",
                positionIdx=0,
            )
            logger.info(f"Позиция по {symbol} закрыта по рынку")
            return True
        except Exception as e:
            logger.error(f"Ошибка закрытия позиции {symbol}: {e}")
            return False