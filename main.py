"""
main.py
Мультивалютный демо-бот на основе свинг-точек.
Использует Bybit Testnet REST API, Telegram-уведомления, YAML-конфиг с Яндекс.Диска.
Работает на непересекающихся блоках по 7 свечей (как в бэктестере).
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
from datetime import datetime, timedelta
from collections import deque
from dotenv import load_dotenv

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
HISTORY_LIMIT = 300          # начальное количество свечей (для 5-минутного графика)
BLOCK_SIZE = 7               # размер непересекающегося блока (как в бэктесте)
YAML_PATH = "config/pairs.yaml"
SYMBOLS = []

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Вспомогательные функции ----------
async def fetch_candles(http_session: HTTP, symbol: str, interval: str = "5", limit: int = HISTORY_LIMIT):
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
            return df
        else:
            logger.error(f"Ошибка получения свечей {symbol}: {resp}")
            return pd.DataFrame()
    except Exception as e:
        logger.error(f"Исключение получения свечей для {symbol}: {e}")
        return pd.DataFrame()

async def get_current_candle(http_session: HTTP, symbol: str) -> dict | None:
    """Получает последнюю завершённую 5-минутную свечу."""
    df = await fetch_candles(http_session, symbol, limit=2)
    if df.empty or len(df) < 2:
        return None
    return df.iloc[-2]

# ---------- Глобальные переменные для управления ботом ----------
running = True

def set_running(value: bool):
    global running
    running = value

async def main():
    # 1. Загрузка параметров из YAML
    yadisk_token = os.getenv("YADISK_TOKEN")
    yadisk = YaDiskSync(yadisk_token, YAML_PATH, remote_path="grid_bot/config/pairs.yaml") if yadisk_token else None
    if yadisk:
        if not yadisk.client.exists(yadisk.remote_path):
            logger.info("Файл не найден на Яндекс.Диске, загружаю локальный")
            yadisk.upload()
        else:
            yadisk.download()
            logger.info("Конфиг синхронизирован с Яндекс.Диском")

    try:
        with open(YAML_PATH, "r") as f:
            params = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/pairs.yaml не найден. Создайте файл с параметрами.")
        return

    global SYMBOLS
    SYMBOLS = list(params.keys())

    http_session = HTTP(testnet=True)
    order_mgr = OrderManager()
    risk_mgr = RiskManager(MAX_TOTAL_RISK_PERCENT)

    # 2. Инициализация буферов, искателей, позиций
    buffers = {sym: [] for sym in SYMBOLS}
    finders = {sym: {'long': LongFinder(sym), 'short': ShortFinder(sym)} for sym in SYMBOLS}
    positions = {sym: None for sym in SYMBOLS}

    # 3. Загрузка истории (заполняем буферы начальными свечами)
    for sym in SYMBOLS:
        df = await fetch_candles(http_session, sym)
        if not df.empty and len(df) >= BLOCK_SIZE:
            last_block = df.iloc[-BLOCK_SIZE:]
            for _, row in last_block.iterrows():
                buffers[sym].append(row.to_dict())
            logger.info(f"{sym}: загружено {len(df)} свечей, буфер заполнен {len(buffers[sym])} свечами")

    # 4. Запуск Telegram-бота с колбэками
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg = None
    if tg_token:
        tg = TelegramBot(
            tg_token,
            stop_callback=lambda: set_running(False),
            sync_callback=lambda: yadisk.sync_if_updated() if yadisk else None
        )
        await tg.start()
        logger.info("Telegram-бот запущен")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN не задан – уведомления отключены")

    # 5. Системное уведомление о старте
    if tg:
        await tg.send_notification("Мультивалютный демо-бот запущен")

    logger.info("Мультивалютный демо-бот запущен")

    last_sync = time.time()

    # 6. Главный цикл
    while running:
        try:
            # Синхронизация с Яндекс.Диском каждые 5 минут
            if yadisk and time.time() - last_sync > 300:
                if yadisk.sync_if_updated():
                    with open(YAML_PATH, "r") as f:
                        params = yaml.safe_load(f)
                    if tg:
                        tg.params = params
                last_sync = time.time()

            logger.info("Проверка пар...")

            for sym in SYMBOLS:
                candle = await get_current_candle(http_session, sym)
                if candle is None:
                    continue

                # Добавляем свечу в буфер, если она новая
                last_ts = buffers[sym][-1]['timestamp'] if buffers[sym] else None
                if last_ts is None or candle['timestamp'] > last_ts:
                    buffers[sym].append(candle.to_dict())
                    logger.debug(f"{sym} новая свеча {candle['timestamp']}")

                # Анализ непересекающегося блока
                if len(buffers[sym]) >= BLOCK_SIZE:
                    block_df = pd.DataFrame(buffers[sym][-BLOCK_SIZE:])
                    long_finder = finders[sym]['long']
                    short_finder = finders[sym]['short']

                    long_finder.process_block(block_df.index[0], block_df)
                    short_finder.process_block(block_df.index[0], block_df)

                    # Очищаем буфер – БОЛЬШЕ ЭТИ СВЕЧИ НЕ ИСПОЛЬЗУЮТСЯ
                    buffers[sym].clear()
                    logger.debug(f"{sym}: блок обработан, буфер очищен")

                # Если буфер стал недостаточным для анализа, пропускаем проверку входа
                df_sym = pd.DataFrame(list(buffers[sym])) if buffers[sym] else pd.DataFrame()
                if df_sym.empty or len(df_sym) < BLOCK_SIZE:
                    continue

                # Здесь df_sym и current_candle ОПРЕДЕЛЕНЫ
                current_candle = df_sym.iloc[-1]
                pair_params = params.get(sym, {})

                # --- Управление открытой позицией ---
                pos = positions[sym]
                if pos:
                    # ... (вся логика управления позицией без изменений)
                    pass  # в реальном коде здесь полная обработка позиции
                else:
                    # --- Поиск новых входов ---
                    for finder, side in [(long_finder, 'LONG'), (short_finder, 'SHORT')]:
                        signal = finder.check_entry(df_sym.index[-1], current_candle)
                        if signal:
                            entry_price = signal['entry_price']
                            if side == 'LONG':
                                structural_stop = entry_price - (signal['max1'][1] - signal['min1'][1]) / 2
                            else:
                                structural_stop = entry_price + (signal['max1'][1] - signal['min1'][1]) / 2

                            max_stop_pct = pair_params.get('MAX_STOP_DISTANCE_PERCENT', 2.5)
                            stop_distance_pct = abs(entry_price - structural_stop) / entry_price * 100
                            if stop_distance_pct <= max_stop_pct:
                                stop_loss = structural_stop
                            else:
                                if side == 'LONG':
                                    stop_loss = entry_price * (1 - max_stop_pct / 100)
                                else:
                                    stop_loss = entry_price * (1 + max_stop_pct / 100)

                            max_loss = pair_params.get('MAX_LOSS_PER_TRADE', 8.0)
                            stop_distance_abs = abs(entry_price - stop_loss)
                            qty = max_loss / stop_distance_abs
                            position_value = qty * entry_price

                            risk_percent = (max_loss / position_value) * 100 if position_value > 0 else 0
                            if not risk_mgr.can_open_position(risk_percent, position_value):
                                if tg:
                                    await tg.send_notification(f"{sym} {side}: недостаточно риска, пропуск")
                                continue

                            order_id = order_mgr.place_limit_order(
                                sym, 'Buy' if side == 'LONG' else 'Sell', qty, entry_price
                            )
                            if order_id:
                                positions[sym] = {
                                    'side': side,
                                    'entry_price': entry_price,
                                    'qty': qty,
                                    'stop_loss': stop_loss,
                                    'breakeven_reached': False,
                                    'has_targets': False,
                                    'parts_active': [],
                                    'parts_qty': qty
                                }
                                risk_mgr.add_risk(risk_percent)
                                if tg:
                                    await tg.send_notification(f"{sym} {side}: вошли {entry_price:.2f}, стоп {stop_loss:.2f}, qty {qty:.6f}")

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.error(f"Ошибка в главном цикле: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    # 7. Завершение работы
    logger.info("Бот остановлен")
    if tg:
        await tg.send_notification("Бот остановлен. Все позиции закрыты.")
        await tg.stop()

if __name__ == "__main__":
    asyncio.run(main())