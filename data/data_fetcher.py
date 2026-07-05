"""
data/data_fetcher.py
Скачивает исторические свечи с Bybit (основная сеть) для бэктеста.
Не требует API-ключей, только публичные данные.

Использование:
  python data/data_fetcher.py
    - загрузит все пары из config.pairs с 1-часовыми свечами за последние 180 дней

  python data/data_fetcher.py --symbol BTCUSDT --interval 60 --start 2025-11-21 --end 2026-05-20
    - загрузит только BTCUSDT с указанным интервалом (в минутах) за период
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import time
import pandas as pd
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP
from config.settings import TIMEFRAME_PRIMARY
from config.pairs import PAIRS_CONFIG
import logging

# Настройка логирования для вывода в консоль
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class HistoricalDataFetcher:
    def __init__(self, symbols, start_date, end_date, interval_minutes):
        self.symbols = symbols
        self.start_date = start_date
        self.end_date = end_date
        self.interval_minutes = interval_minutes          # числовое значение (60, 240...)
        self.interval_str = str(interval_minutes)         # строковое для API Bybit
        self.session = HTTP(testnet=False)
        self.output_dir = "data/historical"
        os.makedirs(self.output_dir, exist_ok=True)

    def fetch_all(self):
        for sym in self.symbols:
            self.fetch_symbol(sym)

    def fetch_symbol(self, symbol):
        file_path = os.path.join(self.output_dir, f"{symbol}_{self.interval_minutes}.csv")
        if os.path.exists(file_path):
            logger.info(f"Файл для {symbol} уже существует, пропускаем.")
            return

        logger.info(f"Загрузка {symbol} за период {self.start_date.date()} - {self.end_date.date()}, интервал {self.interval_minutes} мин")
        all_candles = []
        current_end = int(self.end_date.timestamp() * 1000)
        start_ms = int(self.start_date.timestamp() * 1000)

        while current_end > start_ms:
            try:
                resp = self.session.get_kline(
                    category="linear",
                    symbol=symbol,
                    interval=self.interval_str,
                    end=current_end,
                    limit=200
                )
                if resp['retCode'] != 0:
                    logger.error(f"Ошибка API для {symbol}: {resp}")
                    break

                candles = resp['result']['list']
                if not candles:
                    break

                all_candles.extend(candles)
                oldest_time = int(candles[-1][0])
                if oldest_time <= start_ms:
                    break
                current_end = oldest_time - 1
                logger.debug(f"{symbol}: получено {len(candles)} свечей, всего {len(all_candles)}")
                time.sleep(0.1)
            except Exception as e:
                logger.error(f"Ошибка загрузки {symbol}: {e}")
                break

        if all_candles:
            df = pd.DataFrame(all_candles, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'
            ])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col])
            df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
            df = df.sort_values('timestamp').reset_index(drop=True)
            df.to_csv(file_path, index=False)
            logger.info(f"Сохранено {len(df)} свечей для {symbol} в {file_path}")
        else:
            logger.warning(f"Нет данных для {symbol} за указанный период.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Загрузка исторических свечей Bybit")
    parser.add_argument(
        "--symbol", type=str, nargs='+', default=None,
        help="Торговые пары (например BTCUSDT ETHUSDT). Если не указаны, используются все из config.pairs."
    )
    parser.add_argument(
        "--interval", type=int, default=60,   # <- изменено на 60 по умолчанию
        help="Интервал свечей в минутах (по умолчанию 60)"
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Начальная дата в формате YYYY-MM-DD (по умолчанию 180 дней назад)"
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="Конечная дата в формате YYYY-MM-DD (по умолчанию сегодня)"
    )

    args = parser.parse_args()

    # Список символов
    if args.symbol:
        symbols = args.symbol
    else:
        # Берём все пары из конфига (ключи)
        symbols = list(PAIRS_CONFIG.keys())
        logger.info(f"Загрузка всех пар: {symbols}")

    interval = args.interval

    # Период
    if args.end:
        end_date = datetime.strptime(args.end, "%Y-%m-%d")
    else:
        end_date = datetime.now()
    if args.start:
        start_date = datetime.strptime(args.start, "%Y-%m-%d")
    else:
        start_date = end_date - timedelta(days=180)

    logger.info(f"Параметры: символы={symbols}, интервал={interval} мин, период={start_date.date()} -> {end_date.date()}")

    fetcher = HistoricalDataFetcher(symbols, start_date, end_date, interval)
    fetcher.fetch_all()