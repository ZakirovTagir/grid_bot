"""
backtest/optimizer.py
Оптимизатор: перебирает стратегии и параметры на исторических данных.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import itertools
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Callable
import pandas as pd
import numpy as np

from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics
from config.settings import (
    ADX_FLAT_THRESHOLD, BB_WIDTH_MIN_PCT, AMPLITUDE_CV_MAX, TOUCHES_MIN,
    ADX_TREND_THRESHOLD, ATR_DECAY_THRESHOLD, TRAILING_STOP_PCT
)
from config.pairs import PAIRS_CONFIG

logger = logging.getLogger(__name__)

StrategyFunc = Callable[[pd.DataFrame], str]

class StrategyOptimizer:
    def __init__(self, symbols: List[str], start_date: datetime, end_date: datetime,
                 start_capital: float = 300.0):
        self.symbols = symbols
        self.start_date = start_date
        self.end_date = end_date
        self.start_capital = start_capital
        total_days = (end_date - start_date).days
        self.train_end = start_date + timedelta(days=int(total_days * 0.66))
        self.test_start = self.train_end + timedelta(days=1)

    def run(self):
        best_strategies = {}
        for sym in self.symbols:
            logger.info(f"Оптимизация для {sym}")
            best = self.optimize_for_symbol(sym)
            if best:
                best_strategies[sym] = best
                logger.info(f"Лучшая стратегия для {sym}: {best['name']} с параметрами {best['params']}, "
                            f"CAGR={best['metrics']['cagr']:.2f}%, MDD={best['metrics']['mdd']:.2f}%")
        return best_strategies

    def optimize_for_symbol(self, symbol: str):
        engine = BacktestEngine([symbol], self.start_capital, self.start_date, self.end_date)
        engine.load_data()
        if symbol not in engine.data:
            return None

        strategies = {
            'grid': {
                'func': self.grid_strategy,
                'params': {
                    'adx_flat': [18, 20, 22],
                    'bb_width': [2.0, 2.5, 3.0],
                    'amplitude_cv': [30, 40, 50],
                    'touches': [1, 2, 3],
                    'trailing_stop': [10, 15, 20]
                }
            },
        }

        best_overall = None
        best_score = -999.0

        for strat_name, strat_info in strategies.items():
            param_names = list(strat_info['params'].keys())
            param_values = list(strat_info['params'].values())
            combinations = itertools.product(*param_values)

            for combo in combinations:
                params = dict(zip(param_names, combo))
                original_settings = self._backup_settings()
                self._apply_params(params)

                train_engine = BacktestEngine([symbol], self.start_capital, self.start_date, self.train_end)
                train_engine.load_data()
                if symbol in train_engine.data:
                    train_engine.run(strategy_func=strat_info['func'], quiet=True)
                    if train_engine.results:
                        equity = [self.start_capital]
                        for t in train_engine.results:
                            equity.append(equity[-1] + t['pnl'])
                        metrics = calculate_metrics(equity, train_engine.results, self.start_date, self.train_end)
                        if metrics['mdd'] < 20.0 and metrics['sharpe'] > 0.5:
                            score = metrics['cagr'] + metrics['sharpe'] * 10
                            if score > best_score:
                                best_score = score
                                best_overall = {
                                    'name': strat_name,
                                    'params': params,
                                    'metrics': metrics,
                                    'trades': len(train_engine.results)
                                }
                self._restore_settings(original_settings)

        return best_overall

    def _backup_settings(self):
        return {
            'ADX_FLAT_THRESHOLD': ADX_FLAT_THRESHOLD,
            'BB_WIDTH_MIN_PCT': BB_WIDTH_MIN_PCT,
            'AMPLITUDE_CV_MAX': AMPLITUDE_CV_MAX,
            'TOUCHES_MIN': TOUCHES_MIN,
            'TRAILING_STOP_PCT': TRAILING_STOP_PCT
        }

    def _apply_params(self, params):
        import config.settings as settings
        if 'adx_flat' in params:
            settings.ADX_FLAT_THRESHOLD = params['adx_flat']
        if 'bb_width' in params:
            settings.BB_WIDTH_MIN_PCT = params['bb_width']
        if 'amplitude_cv' in params:
            settings.AMPLITUDE_CV_MAX = params['amplitude_cv']
        if 'touches' in params:
            settings.TOUCHES_MIN = params['touches']
        if 'trailing_stop' in params:
            settings.TRAILING_STOP_PCT = params['trailing_stop']

    def _restore_settings(self, backup):
        import config.settings as settings
        settings.ADX_FLAT_THRESHOLD = backup['ADX_FLAT_THRESHOLD']
        settings.BB_WIDTH_MIN_PCT = backup['BB_WIDTH_MIN_PCT']
        settings.AMPLITUDE_CV_MAX = backup['AMPLITUDE_CV_MAX']
        settings.TOUCHES_MIN = backup['TOUCHES_MIN']
        settings.TRAILING_STOP_PCT = backup['TRAILING_STOP_PCT']

    def grid_strategy(self, df: pd.DataFrame) -> str:
        from core.market_analyzer import is_flat, is_rally
        if not is_rally(df) and is_flat(df):
            return 'BUY'
        return ''