"""
run_backtest.py
Запускает бэктест на исторических данных.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from datetime import datetime, timedelta
from backtest.engine import BacktestEngine
from config.pairs import PAIRS_CONFIG
import logging

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    # Настройка логирования (если ещё не настроено)
    from utils.logger import setup_logging
    setup_logging(level=logging.INFO)

    # Символы для бэктеста (исключаем MATIC, если его нет)
    symbols = [s for s in list(PAIRS_CONFIG.keys())[:10] if s != 'MATICUSDT']

    # Период: последние 6 месяцев
    end_date = datetime.now()
    start_date = end_date - timedelta(days=180)

    engine = BacktestEngine(
        symbols=symbols,
        start_capital=300.0,
        start_date=start_date,
        end_date=end_date
    )

    engine.run()