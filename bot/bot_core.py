"""
bot/bot_core.py
Конечный автомат для управления одной торговой парой.
Состояния: IDLE → SCANNING → PREPARING_GRID → TRADING → CLOSING → COOLDOWN → IDLE
"""
import time
import logging
from enum import Enum
from typing import Optional

from core.market_analyzer import is_rally, is_flat, is_volatility_decay, add_indicators
from core.grid_constructor import calculate_grid
from core.risk_manager import PositionTracker, GlobalRiskMonitor
from core.order_executor import BybitExecutor
from config.settings import COOLDOWN_HOURS, TIMEFRAME_PRIMARY
from utils.telegram import send_message

logger = logging.getLogger("bot_core")

class State(Enum):
    IDLE = 0
    SCANNING = 1
    PREPARING_GRID = 2
    TRADING = 3
    CLOSING = 4
    COOLDOWN = 5

class BotCore:
    def __init__(self, symbol: str, executor: BybitExecutor, risk_monitor: GlobalRiskMonitor,
                 allocated_capital: float):
        self.symbol = symbol
        self.executor = executor
        self.risk_monitor = risk_monitor
        self.allocated_capital = allocated_capital
        self.state = State.IDLE
        self.position_tracker: Optional[PositionTracker] = None
        self.grid_orders = None
        self.last_scan_time = 0
        self.cooldown_end = 0

    def run(self):
        """Главный цикл для одной пары. Вызывается периодически из диспетчера."""
        try:
            if self.state == State.IDLE:
                self._handle_idle()
            elif self.state == State.SCANNING:
                self._handle_scanning()
            elif self.state == State.PREPARING_GRID:
                self._handle_preparing_grid()
            elif self.state == State.TRADING:
                self._handle_trading()
            elif self.state == State.CLOSING:
                self._handle_closing()
            elif self.state == State.COOLDOWN:
                self._handle_cooldown()
        except Exception as e:
            logger.error(f"Ошибка в автомате {self.symbol}: {e}", exc_info=True)
            self._transition_to(State.IDLE)

    def _handle_idle(self):
        if not self.risk_monitor.is_in_cooldown(self.symbol):
            self._transition_to(State.SCANNING)
        else:
            self._transition_to(State.COOLDOWN)

    def _handle_scanning(self):
        """Получить свечи и проверить условия входа."""
        # Получаем свечи 4H (48 штук для анализа)
        df = self._fetch_klines(limit=48)
        if df is None or len(df) < 12:
            return

        # Проверка ралли
        if is_rally(df):
            logger.debug(f"{self.symbol} в ралли, пропускаем")
            return

        # Проверка флета
        if is_flat(df):
            logger.info(f"{self.symbol}: обнаружен боковик, готовим сетку")
            self._transition_to(State.PREPARING_GRID)
        else:
            logger.debug(f"{self.symbol}: нет флета")

    def _handle_preparing_grid(self):
        """Построение и размещение сетки."""
        current_price = self.executor.get_current_price(self.symbol)
        if current_price is None:
            return

        # Определяем границы флета (за 48 часов)
        df = self._fetch_klines(limit=48)
        if df is None or len(df) < 12:
            self._transition_to(State.IDLE)
            return

        upper_bound = df['high'].max()
        lower_bound = df['low'].min()

        # Построение сетки
        grid = calculate_grid(
            symbol=self.symbol,
            upper_bound=upper_bound,
            lower_bound=lower_bound,
            current_price=current_price,
            allocated_capital=self.allocated_capital
        )

        if grid is None:
            logger.info(f"{self.symbol}: не удалось построить сетку (мало уровней)")
            self._transition_to(State.IDLE)
            return

        # Устанавливаем плечо и маржу (однократно)
        self.executor._set_leverage(self.symbol)

        # Размещаем buy и sell ордера
        orders = []
        for order in grid['buy']:
            orders.append({'side': 'Buy', 'price': order['price'], 'qty': order['qty']})
        for order in grid['sell']:
            orders.append({'side': 'Sell', 'price': order['price'], 'qty': order['qty']})

        success = self.executor.place_limit_orders(self.symbol, orders)
        if success:
            self.grid_orders = grid
            self.position_tracker = PositionTracker(
                symbol=self.symbol,
                entry_capital=self.allocated_capital
            )
            send_message(f"✅ Вход в {self.symbol}, сетка из {len(orders)} ордеров")
            logger.info(f"{self.symbol}: сетка размещена, переход в TRADING")
            self._transition_to(State.TRADING)
        else:
            logger.error(f"{self.symbol}: ошибка размещения ордеров")
            self._transition_to(State.IDLE)

    def _handle_trading(self):
        """Мониторинг позиции и условий выхода."""
        # Получить текущий нереализованный PnL
        pos = self.executor.get_positions(self.symbol)
        pnl = pos.get('unrealisedPnl', 0.0) if pos else 0.0

        if self.position_tracker:
            self.position_tracker.update_pnl(pnl)
            # Проверка трейлинг-стопа
            if self.position_tracker.should_trailing_stop():
                logger.info(f"{self.symbol}: сработал трейлинг-стоп, PnL={pnl:.2f}")
                send_message(f"🛑 Трейлинг-стоп {self.symbol}, прибыль {pnl:.2f} USDT")
                self._transition_to(State.CLOSING)
                return

        # Проверка затухания волатильности
        df = self._fetch_klines(limit=48)
        if df is not None and is_volatility_decay(df):
            logger.info(f"{self.symbol}: затухание волатильности, выход")
            send_message(f"📉 Затухание {self.symbol}, выход")
            self._transition_to(State.CLOSING)
            return

        # Проверка смещения границ (раз в 4 часа можно перестраивать)
        # Пока не реализовано для простоты

    def _handle_closing(self):
        """Закрытие всех ордеров и позиции."""
        self.executor.cancel_all_orders(self.symbol)
        self.executor.close_position_market(self.symbol)
        self.risk_monitor.set_cooldown(self.symbol)
        self.grid_orders = None
        self.position_tracker = None
        logger.info(f"{self.symbol}: позиция закрыта, уход в кулдаун")
        self._transition_to(State.COOLDOWN)

    def _handle_cooldown(self):
        if not self.risk_monitor.is_in_cooldown(self.symbol):
            self._transition_to(State.IDLE)
        else:
            remaining = int(self.risk_monitor.cooldowns.get(self.symbol, 0) - time.time())
            logger.debug(f"{self.symbol}: кулдаун ещё {max(0, remaining)} сек")

    def _transition_to(self, new_state: State):
        old_state = self.state
        self.state = new_state
        if old_state != new_state:
            logger.info(f"{self.symbol}: {old_state.name} → {new_state.name}")

    def _fetch_klines(self, limit=48):
        """Загружает свечи 4H через pybit."""
        try:
            resp = self.executor.session.get_kline(
                category="linear",
                symbol=self.symbol,
                interval=TIMEFRAME_PRIMARY,
                limit=limit
            )
            data = resp['result']['list']
            if not data:
                return None
            import pandas as pd
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col])
            df['timestamp'] = pd.to_datetime(pd.to_numeric(df['timestamp']), unit='ms')
            df = df.sort_values('timestamp').reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"Ошибка загрузки свечей {self.symbol}: {e}")
            return None