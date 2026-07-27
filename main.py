"""
main.py
Мультивалютный демо-бот на основе свинг-точек.
Использует процедурный поиск (core/swing_finder.py) с детальным логированием.
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
from core.risk_manager import RiskManager
from core.swing_finder import process_block, check_entry
from utils.telegram_bot import TelegramBot
from utils.yadisk_sync import YaDiskSync

load_dotenv()

# ---------- Глобальные константы ----------
MAX_TOTAL_RISK_PERCENT = 20.0
CHECK_INTERVAL = 30
HISTORY_LIMIT = 100
BLOCK_SIZE = 7
YAML_PATH = "config/pairs.yaml"
SYMBOLS = ['BTCUSDT']  # временно только BTCUSDT

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
        # Проверяем, есть ли уже файл с таким именем, и удаляем (чтобы не дублировать)
        if yadisk.client.exists(remote_path):
            yadisk.client.remove(remote_path)
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

    if 'BTCUSDT' not in params:
        debug_logger.error("BTCUSDT не найден в pairs.yaml")
        return

    http_session = HTTP(testnet=True)
    order_mgr = OrderManager()
    risk_mgr = RiskManager(MAX_TOTAL_RISK_PERCENT)

    sym = 'BTCUSDT'
    state = {
        'long': {'state': 'WAIT_MIN1', 'min1': None, 'max1': None, 'min2': None},
        'short': {'state': 'WAIT_MAX1', 'max1': None, 'min1': None, 'max2': None}
    }

    buffers = {sym: deque(maxlen=HISTORY_LIMIT)}
    last_processed_idx = {sym: -1}
    positions = {sym: None}

    # 3. Загрузка истории
    df = await fetch_candles(http_session, sym)
    if df.empty:
        debug_logger.error(f"{sym}: не удалось загрузить свечи")
        return

    for _, row in df.iterrows():
        buffers[sym].append(row.to_dict())

    total_initial = len(buffers[sym])
    debug_logger.info(f"{sym}: загружено {total_initial} свечей, обрабатываем непересекающиеся блоки")

    for start in range(0, total_initial - BLOCK_SIZE + 1, BLOCK_SIZE):
        end = start + BLOCK_SIZE - 1
        block_df = pd.DataFrame(
            list(buffers[sym])[start:end+1],
            index=range(start, end+1)
        )
        debug_logger.info(f"{sym}: обработка блока {start}-{end} (история)")
        process_block(state, start, block_df, params[sym])
        last_processed_idx[sym] = end

    # Проверяем остаток
    for idx in range(last_processed_idx[sym] + 1, total_initial):
        current_candle = pd.Series(buffers[sym][idx])
        debug_logger.info(f"{sym}: свеча {idx} (история) O={current_candle['open']:.2f} H={current_candle['high']:.2f} L={current_candle['low']:.2f} C={current_candle['close']:.2f}")
        debug_logger.info(f"{sym}: состояние перед check_entry: LONG={state['long']['state']}, SHORT={state['short']['state']}")
        signal = check_entry(state, idx, current_candle, params[sym])
        if signal:
            debug_logger.info(f"{sym}: НАЙДЕН СИГНАЛ ВХОДА на свече {idx} (история)! тип={signal['type']}, цена={signal['entry_price']:.2f}")

    debug_logger.info(f"{sym}: начальная обработка завершена, последний блок до индекса {last_processed_idx[sym]}")

    # 4. Telegram
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg = None
    if tg_token:
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
            # Синхронизация конфига каждые 5 минут (без выгрузки логов)
            if yadisk and time.time() - last_sync > 300:
                if yadisk.sync_if_updated():
                    with open(YAML_PATH, "r", encoding="utf-8") as f:
                        params = yaml.safe_load(f)
                    if tg:
                        tg.params = params
                    debug_logger.info("Конфиг обновлён из Яндекс.Диска")
                last_sync = time.time()

            # Выгрузка логов раз в час (3600 секунд)
            if yadisk and time.time() - last_log_upload > 3600:
                await upload_logs_to_disk(yadisk)
                last_log_upload = time.time()

            debug_logger.debug("Проверка пар...")

            candle = await get_current_candle(http_session, sym)
            if candle is None:
                continue

            last_ts = buffers[sym][-1]['timestamp'] if buffers[sym] else None
            if last_ts is None or candle['timestamp'] > last_ts:
                buffers[sym].append(candle.to_dict())
                debug_logger.info(f"{sym} НОВАЯ СВЕЧА: {candle['timestamp']} O={candle['open']:.2f} H={candle['high']:.2f} L={candle['low']:.2f} C={candle['close']:.2f}")

                total_candles = len(buffers[sym])

                # Проверка нового блока
                if total_candles - 1 - last_processed_idx[sym] >= BLOCK_SIZE:
                    start_idx = last_processed_idx[sym] + 1
                    end_idx = start_idx + BLOCK_SIZE - 1
                    if end_idx < total_candles:
                        block_df = pd.DataFrame(
                            list(buffers[sym])[start_idx:end_idx+1],
                            index=range(start_idx, end_idx+1)
                        )
                        debug_logger.info(f"{sym}: формирование блока {start_idx}-{end_idx} (реальное время)")
                        process_block(state, start_idx, block_df, params[sym])
                        last_processed_idx[sym] = end_idx
                        debug_logger.info(f"{sym}: блок {start_idx}-{end_idx} обработан")

                        # Проверка свечей после блока
                        for idx in range(last_processed_idx[sym] + 1, total_candles):
                            current_candle_check = pd.Series(buffers[sym][idx])
                            debug_logger.info(f"{sym}: проверка входа на свече {idx} (после блока) O={current_candle_check['open']:.2f} H={current_candle_check['high']:.2f} L={current_candle_check['low']:.2f} C={current_candle_check['close']:.2f}")
                            debug_logger.info(f"{sym}: состояние перед check_entry: LONG={state['long']['state']}, SHORT={state['short']['state']}")
                            signal = check_entry(state, idx, current_candle_check, params[sym])
                            if signal:
                                debug_logger.info(f"{sym}: НАЙДЕН СИГНАЛ ВХОДА на свече {idx} (реальное время)!")

                # Проверка входа на только что добавленной свече (между блоками)
                current_idx = len(buffers[sym]) - 1
                current_candle = pd.Series(buffers[sym][current_idx])
                debug_logger.info(f"{sym}: проверка входа на свече {current_idx} (между блоками) O={current_candle['open']:.2f} H={current_candle['high']:.2f} L={current_candle['low']:.2f} C={current_candle['close']:.2f}")
                debug_logger.info(f"{sym}: состояние перед check_entry: LONG={state['long']['state']}, SHORT={state['short']['state']}")
                signal = check_entry(state, current_idx, current_candle, params[sym])
                if signal:
                    debug_logger.info(f"{sym}: НАЙДЕН СИГНАЛ ВХОДА на свече {current_idx} (реальное время)!")

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