"""
run_full_cycle.py
Запуск бэктеста для всех стратегий, включая Bollinger+Donchian.
Для BollDonchian используется рабочая конфигурация + фильтр по ширине полос.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from datetime import datetime, timedelta
from backtest.engine import BacktestEngine
from core.max_trend import max_trend_strict_signal
from utils.logger import setup_logging
import pandas as pd
import logging

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# Параметры для MaxTrend
MAXTREND_PARAMS = {
    'ema_slow': 200,
    'rsi_period': 14,
    'use_trend_lines': False,
    'trend_lookback': 5,
    'merge_pct': 0.5,
    'bounce_tolerance': 0.5,
    'trailing_period': 4
}

# Параметры для Bollinger+Donchian (рабочая конфигурация + фильтр по ширине)
BOLL_DONCHIAN_PARAMS = {
    # Индикаторы
    'bb_period': 10,
    'bb_std': 1.0,
    'donchian_period': 20,
    'fast_breakout_ratio': 0.7,
    # Режим входа
    'force_single_breakout': True,
    'flat_indicator': 'bb',
    # Фильтр по ширине полос (отсекает слишком волатильные периоды)
    'max_bb_width_percent': 4.0,
    # Контекстные фильтры (отключены)
    'use_context_filter': False,
    'use_trend_direction_filter': False,
    'use_volatility_decay_filter': False,
    # Риск и управление позицией
    'risk_per_trade_percent': 3.0,
    'activation_percent': 2.0,
    'breakeven_stop_percent': 1.0,
    'trailing_stop_percent': 2.0,
    # Параметры риск-менеджера
    'max_drawdown_global': 25.0,
    'max_concurrent_positions': 5,
}

def test_boll_donchian(symbol: str, days_back: int = 180, capital: float = 1000.0, params: dict = None):
    if params is None:
        params = BOLL_DONCHIAN_PARAMS.copy()
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    engine = BacktestEngine([symbol], capital, start_date, end_date)
    engine.run(
        strategy_name='BollDonchian',
        params=params,
        quiet=False
    )
    # Сохранение результатов
    if engine.results:
        df_results = pd.DataFrame(engine.results)
        df_results.to_csv('backtest_results.csv', index=False)
        print(f"Сохранено {len(engine.results)} сделок в backtest_results.csv")
    return engine

def test_max_trend(symbol: str, days_back: int = 180, capital: float = 300.0):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    engine = BacktestEngine([symbol], capital, start_date, end_date)
    engine.run(
        strategy_func=max_trend_strict_signal,
        strategy_name='MaxTrend',
        params=MAXTREND_PARAMS,
        quiet=False
    )

def test_grid(symbol: str, days_back: int = 180, capital: float = 300.0):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    engine = BacktestEngine([symbol], capital, start_date, end_date)
    engine.run(strategy_name='Grid', quiet=False)

if __name__ == "__main__":
    logger.info("=== Запуск Bollinger+Donchian (с фильтром BB width <= 4%) ===")
    test_boll_donchian('BTCUSDT', days_back=180, capital=1000.0)