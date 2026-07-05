"""
run_optimizer.py
Запускает оптимизатор стратегий.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from datetime import datetime, timedelta
from backtest.optimizer import StrategyOptimizer
from config.pairs import PAIRS_CONFIG
import logging

if __name__ == "__main__":
    from utils.logger import setup_logging
    setup_logging(level=logging.INFO)

    symbols = [s for s in list(PAIRS_CONFIG.keys())[:10] if s != 'MATICUSDT']
    end_date = datetime.now()
    start_date = end_date - timedelta(days=180)

    optimizer = StrategyOptimizer(symbols, start_date, end_date, start_capital=300.0)
    best = optimizer.run()

    print("\n=== ЛУЧШИЕ СТРАТЕГИИ ===")
    for sym, info in best.items():
        print(f"{sym}: {info['name']} params={info['params']}, "
              f"CAGR={info['metrics']['cagr']:.2f}%, MDD={info['metrics']['mdd']:.2f}%, "
              f"Сделок: {info['trades']}")