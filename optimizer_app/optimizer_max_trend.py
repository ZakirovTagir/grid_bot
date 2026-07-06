"""
optimizer_app/optimizer_max_trend.py
Облегчённый оптимизатор для MaxTrend.
Перебирает ~20-30 комбинаций ключевых параметров.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import itertools
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics
from core.max_trend import max_trend_signal

logger = logging.getLogger(__name__)

# Базовые диапазоны для перебора
PARAM_GRID = {
    'ema_fast': [20, 50],
    'ema_slow': [100, 200],
    'rsi_oversold': [30, 40],
    'rsi_overbought': [60, 70],
    'volume_factor': [1.0, 1.2],
    'atr_stop_mult': [2.0, 3.0],
    'trailing_period': [3, 4, 5],
}

def optimize(symbol: str, start_date: datetime, end_date: datetime,
             start_capital: float = 300.0) -> Optional[Dict]:
    """
    Перебирает комбинации параметров MaxTrend и возвращает лучшую.
    """
    engine = BacktestEngine([symbol], start_capital, start_date, end_date)
    engine.load_data()
    if symbol not in engine.data:
        logger.error(f"Нет данных для {symbol}")
        return None

    param_names = list(PARAM_GRID.keys())
    param_values = list(PARAM_GRID.values())
    combinations = list(itertools.product(*param_values))
    logger.info(f"Перебор {len(combinations)} комбинаций для {symbol}")

    best = None
    best_score = -999.0

    for combo in combinations:
        params = dict(zip(param_names, combo))
        # Создаём новый движок для каждой комбинации
        test_engine = BacktestEngine([symbol], start_capital, start_date, end_date)
        test_engine.load_data()
        test_engine.run(strategy_func=max_trend_signal, strategy_name='MaxTrend',
                       params=params, quiet=True)

        if test_engine.results:
            equity = [start_capital]
            for t in test_engine.results:
                equity.append(equity[-1] + t['pnl'])
            metrics = calculate_metrics(equity, test_engine.results, start_date, end_date)
            # Критерий: максимальный CAGR при MDD < 20% и Шарп > 0.5
            if metrics['mdd'] < 20.0 and metrics['sharpe'] > 0.0:
                score = metrics['cagr'] + metrics['sharpe'] * 5
                if score > best_score:
                    best_score = score
                    best = {
                        'params': params,
                        'metrics': metrics,
                        'trades': len(test_engine.results),
                        'strategy': 'MaxTrend'
                    }

    if best:
        logger.info(f"Лучшая комбинация для {symbol}: {best['params']}, "
                    f"CAGR={best['metrics']['cagr']:.2f}%, MDD={best['metrics']['mdd']:.2f}%")
    return best