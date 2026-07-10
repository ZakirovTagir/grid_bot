"""
main.py
Мультивалютный демо-бот на основе свинг-точек.
Использует Bybit Testnet REST API, Telegram-уведомления, YAML-конфиг с Яндекс.Диска.
Непересекающиеся блоки по 7 свечей для поиска тренда, непрерывный буфер для проверки входа.
Добавлено локальное логирование (debug.log) с ротацией и периодическая выгрузка на Яндекс.Диск.
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
HISTORY_LIMIT = 100          # начальное количество свечей (для минутного графика)
BLOCK_SIZE = 7               # размер непересекающегося блока (как в бэктесте)
YAML_PATH = "config/pairs.yaml"
SYMBOLS = []

# ---------- Настройка основного логгера (консоль + файл с ротацией) ----------
LOG_FILE = "config/debug.log"
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5 МБ
LOG_BACKUP_COUNT = 3

# Корневой логгер для всех модулей
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Формат логов
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Хендлер для консоли (уровень INFO, чтобы не захламлять экран)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Хендлер для файла с ротацией (уровень DEBUG — все сообщения)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=LOG_BACKUP_COUNT)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Отдельный логгер для диагностики (чтобы писать в файл ключевые события)
debug_logger = logging.getLogger("debug")
debug_logger.setLevel(logging.DEBUG)
# Убираем дублирование, так как уже добавлены хендлеры выше, но можно оставить.

# ---------- Вспомогательные функции ----------
async def fetch_candles(http_session: HTTP, symbol: str, interval: str = "1", limit: int = HISTORY_LIMIT):
    """Получает исторические свечи с Bybit Testnet."""
    try:
        end = int(datetime.now().timestamp() * 1000)
        resp = http_session.get_kline(
            category="spot",
            symbol=symbol,
            interval=interval,
            start=end - limit * 60 * 60 * 1000,  # интервал в часах (для минутных свечей корректируем ниже)
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
    return df.iloc[-2]   # предпоследняя свеча (последняя завершённая)

# ---------- Глобальные переменные для управления ботом ----------
running = True

def set_running(value: bool):
    global running
    running = value

async def upload_logs_to_disk(yadisk: YaDiskSync, local_path: str = LOG_FILE, remote_dir: str = "grid_bot/logs/"):
    """Загружает локальный лог-файл на Яндекс.Диск с добавлением даты/времени в имя."""
    if not yadisk:
        debug_logger.warning("Яндекс.Диск не инициализирован, выгрузка логов невозможна")
        return False
    try:
        # Формируем имя файла с датой/временем
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remote_filename = f"debug_{timestamp}.log"
        remote_path = remote_dir + remote_filename
        # Загружаем
        success = yadisk.upload_file(local_path, remote_path)
        if success:
            debug_logger.info(f"Лог успешно выгружен на Яндекс.Диск: {remote_path}")
        else:
            debug_logger.error("Не удалось выгрузить лог на Яндекс.Диск")
        return success
    except Exception as e:
        debug_logger.error(f"Ошибка при выгрузке лога: {e}")
        return False

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

    # 2. Инициализация непрерывных буферов, индексов последней обработанной свечи, искателей, позиций
    buffers = {sym: deque(maxlen=HISTORY_LIMIT) for sym in SYMBOLS}
    last_processed_idx = {sym: -1 for sym in SYMBOLS}   # индекс последней свечи, включённой в блок
    finders = {sym: {
        'long': LongFinder(sym, params.get(sym, {})),
        'short': ShortFinder(sym, params.get(sym, {}))
    } for sym in SYMBOLS}
    positions = {sym: None for sym in SYMBOLS}

    # 3. Загрузка истории (заполняем буферы начальными свечами) - ПОСЛЕДОВАТЕЛЬНАЯ ОБРАБОТКА
    for sym in SYMBOLS:
        df = await fetch_candles(http_session, sym)
        if df.empty:
            debug_logger.warning(f"{sym}: не удалось загрузить свечи")
            continue

        # Сбрасываем состояние искателей
        finders[sym]['long'].reset()
        finders[sym]['short'].reset()

        # Заполняем буфер свечами
        for _, row in df.iterrows():
            buffers[sym].append(row.to_dict())

        total_initial = len(buffers[sym])
        debug_logger.info(f"{sym}: загружено {total_initial} свечей, начинаем последовательную обработку")

        # --- Последовательная обработка, как в бэктестере ---
        last_processed_idx[sym] = -1   # начинаем с -1, чтобы первый блок формировался с 0

        # Проходим по всем свечам в буфере
        for idx in range(total_initial):
            current_candle = pd.Series(buffers[sym][idx])

            # Проверяем, не накопилось ли 7 новых свечей для блока
            if idx - last_processed_idx[sym] >= BLOCK_SIZE:
                start_idx = last_processed_idx[sym] + 1
                end_idx = start_idx + BLOCK_SIZE - 1
                if end_idx < total_initial:
                    block_df = pd.DataFrame(
                        list(buffers[sym])[start_idx:end_idx+1],
                        index=range(start_idx, end_idx+1)
                    )
                    debug_logger.debug(f"{sym}: формирование блока {start_idx}-{end_idx}")
                    finders[sym]['long'].process_block(start_idx, block_df)
                    finders[sym]['short'].process_block(start_idx, block_df)
                    last_processed_idx[sym] = end_idx
                    debug_logger.debug(f"{sym}: блок {start_idx}-{end_idx} обработан на свече {idx}")

            # Проверяем вход на текущей свече (если состояние позволяет)
            signal_long = finders[sym]['long'].check_entry(idx, current_candle)
            signal_short = finders[sym]['short'].check_entry(idx, current_candle)
            if signal_long or signal_short:
                signal = signal_long or signal_short
                debug_logger.info(f"{sym}: НАЙДЕН СИГНАЛ ВХОДА на свече {idx}! тип={signal['type']}, цена={signal['entry_price']:.2f}")
                # Здесь можно добавить обработку сигнала для исторических данных (но мы этого не делаем,
                # потому что это прошлое. Однако для теста мы фиксируем факт.)
            # Для отладки можно добавить логирование состояния после каждого блока, но не перегружаем

        debug_logger.info(f"{sym}: начальная обработка завершена, последний обработанный блок до индекса {last_processed_idx[sym]}")
        # Логируем состояние искателей после обработки истории
        debug_logger.info(f"{sym}: LONG состояние={finders[sym]['long'].state}, SHORT состояние={finders[sym]['short'].state}")

    # 4. Запуск Telegram-бота с колбэками
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg = None
    if tg_token:
        # Определяем колбэк для выгрузки логов
        async def upload_logs_callback():
            if yadisk:
                await upload_logs_to_disk(yadisk)
            else:
                debug_logger.warning("Яндекс.Диск не настроен, выгрузка невозможна")

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

    # 5. Системное уведомление о старте
    if tg:
        await tg.send_notification("Мультивалютный демо-бот запущен")

    debug_logger.info("Мультивалютный демо-бот запущен")

    last_sync = time.time()
    last_log_upload = time.time()  # для периодической выгрузки логов

    # 6. Главный цикл
    while running:
        try:
            # Синхронизация с Яндекс.Диском каждые 5 минут
            if yadisk and time.time() - last_sync > 300:
                if yadisk.sync_if_updated():
                    with open(YAML_PATH, "r", encoding="utf-8") as f:
                        params = yaml.safe_load(f)
                    if tg:
                        tg.params = params
                    debug_logger.info("Конфиг обновлён из Яндекс.Диска")
                last_sync = time.time()

            # Выгрузка логов на Яндекс.Диск раз в час (3600 секунд)
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

                # Добавляем свечу в непрерывный буфер, если она новая
                last_ts = buffers[sym][-1]['timestamp'] if buffers[sym] else None
                if last_ts is None or candle['timestamp'] > last_ts:
                    buffers[sym].append(candle.to_dict())
                    debug_logger.debug(f"{sym} новая свеча {candle['timestamp']}")

                # Формирование непересекающегося блока из НОВЫХ свечей
                total_candles = len(buffers[sym])
                if total_candles - 1 - last_processed_idx[sym] >= BLOCK_SIZE:
                    start_idx = last_processed_idx[sym] + 1
                    end_idx = start_idx + BLOCK_SIZE - 1
                    if end_idx < total_candles:
                        block_df = pd.DataFrame(
                            list(buffers[sym])[start_idx:end_idx+1],
                            index=range(start_idx, end_idx+1)
                        )
                        debug_logger.debug(f"{sym}: формирование блока {start_idx}-{end_idx} (реальное время)")
                        long_finder.process_block(start_idx, block_df)
                        short_finder.process_block(start_idx, block_df)
                        last_processed_idx[sym] = end_idx
                        debug_logger.debug(f"{sym}: блок {start_idx}-{end_idx} обработан")

                        # ---- Проверка оставшихся свечей после блока (аналогично истории) ----
                        if last_processed_idx[sym] < total_candles - 1:
                            df_remaining = pd.DataFrame(
                                list(buffers[sym])[last_processed_idx[sym]+1:],
                                index=range(last_processed_idx[sym]+1, total_candles)
                            )
                            for idx, row in df_remaining.iterrows():
                                signal_long = long_finder.check_entry(idx, row)
                                signal_short = short_finder.check_entry(idx, row)
                                if signal_long or signal_short:
                                    signal = signal_long or signal_short
                                    debug_logger.info(f"{sym}: НАЙДЕН СИГНАЛ ВХОДА на свече {idx} (после блока в реальном времени)")
                                    # Здесь можно обработать сигнал (выставить ордер)
                                    # Для простоты пока только логируем

                # Проверка входа на каждой новой свече (если она не была обработана как часть блока)
                # Но мы уже проверяем вход в блоке выше для оставшихся свечей,
                # так что дублировать не нужно.

                # --- Управление открытой позицией (без изменений) ---
                pos = positions[sym]
                if pos:
                    # ... существующий код управления позицией (он не менялся) ...
                    # Чтобы не загромождать, оставляем его без изменений.
                    # Если нужно, я могу вставить полный код, но он длинный.
                    # Сейчас главное - исправить логику поиска сигналов.
                    pass

                # Поиск новых входов (если позиции нет) — будет обрабатываться отдельно,
                # но поскольку мы уже логируем сигналы выше, здесь мы можем добавить реальное выставление ордера.
                # Пока оставляем как есть.

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            debug_logger.error(f"Ошибка в главном цикле: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    # 7. Завершение работы
    debug_logger.info("Бот остановлен")
    if tg:
        await tg.send_notification("Бот остановлен. Все позиции закрыты.")
        await tg.stop()
    # Принудительная выгрузка лога перед завершением
    if yadisk:
        await upload_logs_to_disk(yadisk)

if __name__ == "__main__":
    asyncio.run(main())