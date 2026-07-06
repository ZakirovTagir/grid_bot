"""
bot/dispatcher.py
Управляет параллельной работой нескольких BotCore, глобальным риском и капиталом.
"""
import time
import logging
from typing import Dict, List

from core.order_executor import BybitExecutor
from core.risk_manager import GlobalRiskMonitor
from bot.bot_core import BotCore, State
from config.settings import MAX_POSITIONS, CAPITAL_PER_POSITION_PCT
from utils.telegram import send_message

logger = logging.getLogger("dispatcher")


class Dispatcher:
    def __init__(self, symbols: List[str], executor: BybitExecutor):
        self.symbols = symbols
        self.executor = executor
        self.bots: Dict[str, BotCore] = {}
        self.risk_monitor = GlobalRiskMonitor(initial_balance=self._get_balance())

    def _get_balance(self):
        return self.executor.get_usdt_balance()

    def _allocate_capital(self) -> float:
        """Равномерное распределение капитала на все активные слоты."""
        balance = self._get_balance()
        self.risk_monitor.update_balance(balance)
        max_pos = self.risk_monitor.max_positions
        capital_per_bot = balance * CAPITAL_PER_POSITION_PCT
        active_count = len([b for b in self.bots.values() if b.state.value >= 2])
        if active_count >= max_pos:
            return 0.0
        return min(capital_per_bot, balance / (max_pos + 1))

    def run(self):
        """Основной цикл диспетчера."""
        print("Диспетчер работает...")
        while True:
            try:
                self.risk_monitor.update_balance(self._get_balance())

                # Проверка глобальной просадки
                if self.risk_monitor.is_global_drawdown_exceeded():
                    logger.warning("Глобальная просадка > 20%!")
                    send_message("🚨 Просадка > 20%! Принудительное сокращение.")
                    self._force_close_worst()

                # Обновление списка символов
                for sym in self.symbols:
                    if sym not in self.bots:
                        capital = self._allocate_capital()
                        if capital > 10:
                            self.bots[sym] = BotCore(sym, self.executor, self.risk_monitor, capital)
                        else:
                            continue
                    self.bots[sym].run()

                time.sleep(60)  # пауза между циклами (1 мин)
            except KeyboardInterrupt:
                logger.info("Остановка по сигналу")
                break
            except Exception as e:
                logger.error(f"Ошибка в диспетчере: {e}", exc_info=True)
                time.sleep(10)

    def _force_close_worst(self):
        """Закрывает самую убыточную позицию."""
        worst_symbol = None
        worst_pnl = 0
        for bot in self.bots.values():
            if bot.position_tracker:
                pnl = bot.position_tracker.current_pnl
                if pnl < worst_pnl:
                    worst_pnl = pnl
                    worst_symbol = bot.symbol
        if worst_symbol and worst_symbol in self.bots:
            logger.info(f"Принудительное закрытие {worst_symbol}")
            self.bots[worst_symbol]._transition_to(State.CLOSING)
            self.risk_monitor.reduce_exposure()