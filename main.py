"""
main.py
Мультивалютный демо-бот на основе свинг-точек.
Использует Bybit Testnet REST API, Telegram-уведомления, YAML-конфиг с Яндекс.Диска.
Перекрывающиеся блоки (скользящее окно) для поиска тренда — как в бэктестере.
"""
from __future__ import annotations
import sys

# ---------- ПАТЧ pybit для Python 3.8 ----------
if sys.version_info < (3, 9):
    import subprocess, os
    lib_dir = os.path.join(os.path.dirname(__file__), 'venv', 'lib',
                           f'python{sys.version_info.major}.{sys.version_info.minor}',
                           'site-packages', 'pybit')
    if os.path.exists(lib_dir):
        subprocess.run(
            f"find {lib_dir} -name '*.py' -exec sed -i 's/defaultdict\\[dict\\]/defaultdict/g' {{}} \\;",
            shell=True, check=False
        )
# ------------------------------------------------

import asyncio
import logging
import os
import time
import yaml
import pandas as pd
from datetime import datetime
from collections import deque
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

from pybit.unified_trading import HTTP
from core.order_manager import OrderManager
from core.swing_trader import LongFinder, ShortFinder
from core.risk_manager import RiskManager
from utils.telegram_bot import TelegramBot
from utils.yadisk_sync import YaDiskSync

load_dotenv()

# ---------- Глобальные константы ----------
MAX_TOTAL_RISK_PERCENT = 20.0
CHECK_INTERVAL = 30          # секунд между проверками
HISTORY_LIMIT = 100          # начальное количество свечей
BLOCK_SIZE = 7               # размер окна (скользящее)
YAML_PATH = "config/pairs.yaml"
SYMBOLS = []

# ---------- Настройка логирования ----------
LOG_FILE = "config/debug.log"
LOG_MAX_SIZE = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=LOG_BACKUP_COUNT)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

debug_logger = logging.getLogger("debug")
debug_logger.setLevel(logging.DEBUG)

# ---------- Вспомогательные функции ----------
async def fetch_candles(http_session: HTTP, symbol: str, interval: str = "1", limit: int = HISTORY_LIMIT):
    """Получает исторические свечи с Bybit Testnet."""
    try:
        end = int(datetime.now().timestamp() * 1000)
        resp = http_session.get_kline(
            category="spot",
            symbol=symbol,
            interval=interval,
            start=end - limit * 60 * 60 * 1000,
            end=end,
            limit=limit
        )
        if resp.get("retCode") == 0:
            data = resp["result"]["list"]
            if not data:
                return pd.DataFrame()
            candles = []
            for item in data:
                if len(item) < 6:
                    continue
                ts = int(item[0])
                open_ = float(item[1])
                high = float(item[2])
                low = float(item[3])
                close = float(item[4])
                volume = float(item[5])
                candles.append({
                    "timestamp": pd.Timestamp(ts, unit='ms'),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume
                })
            df = pd.DataFrame(candles).sort_values("timestamp")
            debug_logger.debug(f"Загружено {len(df)} свечей для {symbol}")
            return df
        else:
            debug_logger.error(f"Ошибка получения свечей {symbol}: {resp}")
            return pd.DataFrame()
    except Exception as e:
        debug_logger.error(f"Исключение получения свечей для {symbol}: {e}")
        return pd.DataFrame()

async def get_current_candle(http_session: HTTP, symbol: str) -> dict | None:
    """Получает последнюю завершённую минутную свечу."""
    df = await fetch_candles(http_session, symbol, limit=2)
    if df.empty or len(df) < 2:
        return None
    return df.iloc[-2]

# ---------- Управление ботом ----------
running = True

def set_running(value: bool):
    global running
    running = value

async def upload_logs_to_disk(yadisk: YaDiskSync, local_path: str = LOG_FILE, remote_dir: str = "grid_bot/logs/"):
    if not yadisk:
        debug_logger.warning("Яндекс.Диск не инициализирован, выгрузка логов невозможна")
        return False
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remote_filename = f"debug_{timestamp}.log"
        remote_path = remote_dir + remote_filename
        success = yadisk.upload_file(local_path, remote_path)
        if success:
            debug_logger.info(f"Лог успешно выгружен на Яндекс.Диск: {remote_path}")
        else:
            debug_logger.error("Не удалось выгрузить лог на Яндекс.Диск")
        return success
    except Exception as e:
        debug_logger.error(f"Ошибка при выгрузке лога: {e}")
        return False

# ---------- Основная функция ----------
async def main():
    # 1. Загрузка параметров из YAML
    yadisk_token = os.getenv("YADISK_TOKEN")
    yadisk = YaDiskSync(yadisk_token, YAML_PATH, remote_path="grid_bot/config/pairs.yaml") if yadisk_token else None
    if yadisk:
        if not yadisk.client.exists(yadisk.remote_path):
            debug_logger.info("Файл не найден на Яндекс.Диске, загружаю локальный")
            yadisk.upload()
        else:
            yadisk.download()
            debug_logger.info("Конфиг синхронизирован с Яндекс.Диском")

    try:
        with open(YAML_PATH, "r", encoding="utf-8") as f:
            params = yaml.safe_load(f)
    except FileNotFoundError:
        debug_logger.error("config/pairs.yaml не найден. Создайте файл с параметрами.")
        return

    global SYMBOLS
    SYMBOLS = list(params.keys())
    debug_logger.info(f"Загружены символы: {SYMBOLS}")

    http_session = HTTP(testnet=True)
    order_mgr = OrderManager()
    risk_mgr = RiskManager(MAX_TOTAL_RISK_PERCENT)

    # 2. Инициализация буферов и искателей
    buffers = {sym: deque(maxlen=HISTORY_LIMIT) for sym in SYMBOLS}
    finders = {sym: {
        'long': LongFinder(sym, params.get(sym, {})),
        'short': ShortFinder(sym, params.get(sym, {}))
    } for sym in SYMBOLS}
    positions = {sym: None for sym in SYMBOLS}

    # 3. Загрузка истории и обработка через скользящее окно
    for sym in SYMBOLS:
        df = await fetch_candles(http_session, sym)
        if df.empty:
            debug_logger.warning(f"{sym}: не удалось загрузить свечи")
            continue

        # Сбрасываем состояние искателей
        finders[sym]['long'].reset()
        finders[sym]['short'].reset()

        # Заполняем буфер
        for _, row in df.iterrows():
            buffers[sym].append(row.to_dict())

        total_initial = len(buffers[sym])
        debug_logger.info(f"{sym}: загружено {total_initial} свечей, обрабатываем скользящим окном")

        # Проходим по всем свечам, начиная с индекса, где есть 7 свечей
        for idx in range(BLOCK_SIZE - 1, total_initial):
            # Формируем окно из последних 7 свечей (включая текущую)
            start_idx = idx - BLOCK_SIZE + 1
            end_idx = idx
            block_df = pd.DataFrame(
                list(buffers[sym])[start_idx:end_idx+1],
                index=range(start_idx, end_idx+1)
            )
            # Обрабатываем блок искателями
            finders[sym]['long'].process_block(start_idx, block_df)
            finders[sym]['short'].process_block(start_idx, block_df)

            # Проверяем вход на текущей свече
            current_candle = pd.Series(buffers[sym][idx])
            signal_long = finders[sym]['long'].check_entry(idx, current_candle)
            signal_short = finders[sym]['short'].check_entry(idx, current_candle)
            if signal_long or signal_short:
                signal = signal_long or signal_short
                debug_logger.info(f"{sym}: НАЙДЕН СИГНАЛ ВХОДА на свече {idx} (история)! тип={signal['type']}, цена={signal['entry_price']:.2f}")

        debug_logger.info(f"{sym}: начальная обработка завершена")
        debug_logger.info(f"{sym}: LONG состояние={finders[sym]['long'].state}, SHORT состояние={finders[sym]['short'].state}")

    # 4. Запуск Telegram-бота
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg = None
    if tg_token:
        # Колбэк для выгрузки логов (возвращает bool)
        async def upload_logs_callback():
            if yadisk:
                return await upload_logs_to_disk(yadisk)
            else:
                debug_logger.warning("Яндекс.Диск не настроен, выгрузка невозможна")
                return False

        tg = TelegramBot(
            tg_token,
            stop_callback=lambda: set_running(False),
            sync_callback=lambda: yadisk.sync_if_updated() if yadisk else None,
            upload_logs_callback=upload_logs_callback
        )
        await tg.start()
        debug_logger.info("Telegram-бот запущен")
    else:
        debug_logger.warning("TELEGRAM_BOT_TOKEN не задан – уведомления отключены")

    if tg:
        await tg.send_notification("Мультивалютный демо-бот запущен")

    debug_logger.info("Мультивалютный демо-бот запущен")

    last_sync = time.time()
    last_log_upload = time.time()

    # 5. Главный цикл
    while running:
        try:
            # Синхронизация с Яндекс.Диском
            if yadisk and time.time() - last_sync > 300:
                if yadisk.sync_if_updated():
                    with open(YAML_PATH, "r", encoding="utf-8") as f:
                        params = yaml.safe_load(f)
                    if tg:
                        tg.params = params
                    debug_logger.info("Конфиг обновлён из Яндекс.Диска")
                last_sync = time.time()

            # Выгрузка логов раз в час
            if yadisk and time.time() - last_log_upload > 3600:
                await upload_logs_to_disk(yadisk)
                last_log_upload = time.time()

            debug_logger.debug("Проверка пар...")

            for sym in SYMBOLS:
                long_finder = finders[sym]['long']
                short_finder = finders[sym]['short']
                candle = await get_current_candle(http_session, sym)
                if candle is None:
                    continue

                # Добавляем свечу, если она новая
                last_ts = buffers[sym][-1]['timestamp'] if buffers[sym] else None
                if last_ts is None or candle['timestamp'] > last_ts:
                    buffers[sym].append(candle.to_dict())
                    debug_logger.debug(f"{sym} новая свеча {candle['timestamp']}")

                    # Если в буфере достаточно свечей, формируем скользящее окно
                    if len(buffers[sym]) >= BLOCK_SIZE:
                        # Берём последние 7 свечей
                        start_idx = len(buffers[sym]) - BLOCK_SIZE
                        end_idx = len(buffers[sym]) - 1
                        block_df = pd.DataFrame(
                            list(buffers[sym])[start_idx:end_idx+1],
                            index=range(start_idx, end_idx+1)
                        )
                        # Обрабатываем блок
                        long_finder.process_block(start_idx, block_df)
                        short_finder.process_block(start_idx, block_df)

                        # Проверяем вход на последней свече
                        current_idx = end_idx
                        current_candle = pd.Series(buffers[sym][current_idx])
                        signal_long = long_finder.check_entry(current_idx, current_candle)
                        signal_short = short_finder.check_entry(current_idx, current_candle)
                        if signal_long or signal_short:
                            signal = signal_long or signal_short
                            debug_logger.info(f"{sym}: НАЙДЕН СИГНАЛ ВХОДА на свече {current_idx} (реальное время)!")
                            # Здесь можно добавить логику выставления ордера
                            # (пока только логируем для теста)

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            debug_logger.error(f"Ошибка в главном цикле: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    # 6. Завершение
    debug_logger.info("Бот остановлен")
    if tg:
        await tg.send_notification("Бот остановлен. Все позиции закрыты.")
        await tg.stop()
    if yadisk:
        await upload_logs_to_disk(yadisk)

if __name__ == "__main__":
    asyncio.run(main())