"""
main.py
Live-бот на 15m BTCUSDT. Реальные ордера на Bybit Demo Trading (linear perpetual, leverage=1x).

Логика:
1. Свечи читаются с mainnet spot (публичные данные, ключи не нужны).
2. OrderManager работает на Demo Trading linear (LONG и SHORT).
3. При сигнале:
   - считает размер позиции от MAX_LOSS_PER_TRADE
   - проверяет цель (ручная из manual_targets.json или формула)
   - открывает позицию по рынку
   - выставляет стоп-маркет reduceOnly
4. Каждый цикл:
   - если позиции нет — ищет сигнал
   - если позиция есть — проверяет target и следит за закрытием биржей
5. После закрытия — обнуляет manual target и очищает live_position.json.
6. Раз в час и через ~90 сек после старта — выгружает debug.log и journalctl
   на Яндекс.Диск; при старте дополнительно шлёт health-check в Telegram.
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
import json
import time
import subprocess  # [+] PATCH 1: для journalctl
import yaml
import pandas as pd
from datetime import datetime
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
CANDLE_INTERVAL = "15"
YAML_PATH = "config/pairs.yaml"
MANUAL_TARGETS_FILE = "config/manual_targets.json"
LIVE_POSITION_FILE = "config/live_position.json"
SYMBOLS = ['BTCUSDT']

INTERVAL_MS = {
    "1": 60 * 1000, "3": 3 * 60 * 1000, "5": 5 * 60 * 1000,
    "15": 15 * 60 * 1000, "30": 30 * 60 * 1000,
    "60": 60 * 60 * 1000, "120": 120 * 60 * 1000,
    "240": 240 * 60 * 1000, "D": 24 * 60 * 60 * 1000,
}

# ---------- Логирование ----------
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

for noisy in ("telegram", "telegram.ext", "telegram.ext.ExtBot",
              "telegram.ext.Updater", "telegram.ext.Application",
              "httpx", "httpcore", "pybit", "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------- Вспомогательные ----------
def load_manual_target() -> float:
    """Ручная цель из config/manual_targets.json (0 = не задана)."""
    if not os.path.exists(MANUAL_TARGETS_FILE):
        return 0.0
    try:
        with open(MANUAL_TARGETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("BTCUSDT", 0.0) or 0.0)
    except Exception:
        return 0.0


def clear_manual_target() -> None:
    """Обнуляет ручную цель после закрытия сделки."""
    try:
        data = {}
        if os.path.exists(MANUAL_TARGETS_FILE):
            with open(MANUAL_TARGETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["BTCUSDT"] = 0.0
        with open(MANUAL_TARGETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        debug_logger.info("Ручная цель BTCUSDT обнулена")
    except Exception as e:
        debug_logger.error(f"clear_manual_target failed: {e}")


def load_live_position() -> dict | None:
    if not os.path.exists(LIVE_POSITION_FILE):
        return None
    try:
        with open(LIVE_POSITION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if data else None
    except Exception:
        return None


def save_live_position(pos: dict) -> None:
    with open(LIVE_POSITION_FILE, "w", encoding="utf-8") as f:
        json.dump(pos, f, indent=2)


def clear_live_position() -> None:
    if os.path.exists(LIVE_POSITION_FILE):
        os.remove(LIVE_POSITION_FILE)


def calc_structural_target(side: str, min1: float, max1: float,
                           min2: float, max2: float) -> float:
    """LONG: min2 + (max1-min1)/2. SHORT: max2 - (max1-min1)/2."""
    impulse = max1 - min1
    if impulse <= 0:
        return 0.0
    if side == 'LONG':
        return min2 + impulse / 2.0
    else:
        return max2 - impulse / 2.0


async def fetch_candles(http_session: HTTP, symbol: str,
                        interval: str = CANDLE_INTERVAL,
                        limit: int = HISTORY_LIMIT):
    """Свечи с mainnet spot — публичный endpoint, ключи не нужны."""
    try:
        end = int(datetime.now().timestamp() * 1000)
        step_ms = INTERVAL_MS.get(interval, 60 * 1000)
        start = end - limit * step_ms
        resp = http_session.get_kline(
            category="spot", symbol=symbol, interval=interval,
            start=start, end=end, limit=limit,
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
                candles.append({
                    "timestamp": pd.Timestamp(ts, unit='ms'),
                    "open": float(item[1]), "high": float(item[2]),
                    "low": float(item[3]), "close": float(item[4]),
                    "volume": float(item[5]),
                })
            return pd.DataFrame(candles).sort_values("timestamp")
        else:
            debug_logger.error(f"Ошибка свечей {symbol}: {resp}")
            return pd.DataFrame()
    except Exception as e:
        debug_logger.error(f"Исключение свечей {symbol}: {e}")
        return pd.DataFrame()


async def get_current_candle(http_session: HTTP, symbol: str):
    df = await fetch_candles(http_session, symbol, limit=2)
    if df.empty or len(df) < 2:
        return None
    return df.iloc[-2]


async def upload_logs_to_disk(yadisk: YaDiskSync, local_path: str = LOG_FILE,
                              remote_dir: str = "grid_bot/logs/"):
    if not yadisk:
        return False
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remote_path = remote_dir + f"debug_{timestamp}.log"
        if yadisk.client.exists(remote_path):
            yadisk.client.remove(remote_path)
        success = yadisk.upload_file(local_path, remote_path)
        if success:
            debug_logger.info(f"Лог выгружен: {remote_path}")
        return success
    except Exception as e:
        debug_logger.error(f"Ошибка выгрузки лога: {e}")
        return False


# [+] PATCH 3: выгрузка journalctl на Я.Диск
def upload_journal_to_disk(yadisk, since_ts: int,
                           remote_dir: str = "grid_bot/logs/"):
    """
    Выгружает journalctl для gridbot.service с момента since_ts (unix) на Я.Диск.
    Возвращает remote_path или None.
    """
    if not yadisk:
        return None
    local_path = None
    try:
        ts_str = datetime.fromtimestamp(since_ts).strftime("%Y%m%d_%H%M%S")
        local_path = f"config/journal_{ts_str}.log"
        remote_path = f"{remote_dir}journal_{ts_str}.log"

        with open(local_path, "w") as f:
            subprocess.run(
                ["journalctl", "-u", "gridbot",
                 "--since", f"@{since_ts}",
                 "--no-pager", "-o", "short-iso"],
                stdout=f, stderr=subprocess.PIPE,
                check=False, timeout=30,
            )

        if yadisk.upload_file(local_path, remote_path):
            debug_logger.info(f"journal выгружен: {remote_path}")
            return remote_path
        debug_logger.error("Не удалось выгрузить journal на Я.Диск")
        return None
    except Exception as e:
        debug_logger.error(f"upload_journal failed: {e}")
        return None
    finally:
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass


# [+] PATCH 4: стартовый health-check + снимок журнала
async def startup_snapshot_and_healthcheck(order_mgr, tg, yadisk, start_ts):
    """
    Через ~90 сек после старта:
    - проверяет доступность Bybit / Я.Диск
    - выгружает journalctl с момента старта на Я.Диск
    - шлёт одно короткое сообщение в TG
    """
    await asyncio.sleep(90)

    lines = []

    # Bybit
    balance = order_mgr.get_wallet_usdt()
    if balance is not None:
        lines.append(f"OK Bybit API (баланс {balance:.2f} USDT)")
    else:
        lines.append("FAIL Bybit API (нет ответа)")

    # Я.Диск
    if yadisk is not None:
        try:
            if yadisk.client.exists("grid_bot/config/pairs.yaml"):
                lines.append("OK Я.Диск")
            else:
                lines.append("FAIL Я.Диск (нет доступа к config)")
        except Exception as e:
            lines.append(f"FAIL Я.Диск ({e})")
    else:
        lines.append("SKIP Я.Диск (токен не задан)")

    # journalctl → Я.Диск
    remote_path = upload_journal_to_disk(yadisk, start_ts)
    if remote_path:
        lines.append("OK journal (выгружен)")
    else:
        lines.append("FAIL journal (см. debug.log)")

    # Telegram — в самом конце, чтобы был и индикатором, и отчётом
    if tg is not None:
        try:
            text = "Стартовый снимок:\n" + "\n".join(lines)
            if remote_path:
                text += f"\n\nЛог: {remote_path}"
            await tg.send_notification(text)
        except Exception as e:
            debug_logger.error(f"startup healthcheck: TG send failed: {e}")


# ---------- Управление ----------
running = True


def set_running(value: bool):
    global running
    running = value


# ---------- Основная функция ----------
async def main():
    # 1. Яндекс.Диск и конфиг
    yadisk_token = os.getenv("YADISK_TOKEN")
    yadisk = YaDiskSync(yadisk_token, YAML_PATH,
                        remote_path="grid_bot/config/pairs.yaml") if yadisk_token else None
    if yadisk:
        if not yadisk.client.exists(yadisk.remote_path):
            yadisk.upload()
        else:
            yadisk.download()
            debug_logger.info("Конфиг синхронизирован с Яндекс.Диском")

    try:
        with open(YAML_PATH, "r", encoding="utf-8") as f:
            params = yaml.safe_load(f)
    except FileNotFoundError:
        debug_logger.error("config/pairs.yaml не найден")
        return

    if 'BTCUSDT' not in params:
        debug_logger.error("BTCUSDT не найден в pairs.yaml")
        return

    def reload_params_from_disk():
        try:
            with open(YAML_PATH, "r", encoding="utf-8") as f:
                new_params = yaml.safe_load(f)
            params.clear()
            params.update(new_params)
            debug_logger.info("Параметры перечитаны из pairs.yaml")
        except Exception as e:
            debug_logger.error(f"reload_params_from_disk failed: {e}")
            raise

    def upload_params_to_disk():
        if not yadisk:
            return
        try:
            if yadisk.upload():
                debug_logger.info("pairs.yaml загружен на Яндекс.Диск")
        except Exception as e:
            debug_logger.error(f"upload_params_to_disk failed: {e}")
            raise

    # 2. Сессии
    http_session = HTTP(testnet=False)     # публичные данные для свечей
    order_mgr = OrderManager()             # Demo Trading linear, ключи из .env
    risk_mgr = RiskManager(MAX_TOTAL_RISK_PERCENT)

    sym = 'BTCUSDT'

    # Установить плечо 1x один раз
    order_mgr.set_leverage(sym, leverage=1)

    state = {
        'long': {'state': 'WAIT_MIN1', 'min1': None, 'max1': None, 'min2': None},
        'short': {'state': 'WAIT_MAX1', 'max1': None, 'min1': None, 'max2': None}
    }

    buffers = {sym: []}
    last_processed_idx = {sym: -1}

    # 3. Стартовая загрузка истории
    df = await fetch_candles(http_session, sym)
    if df.empty:
        debug_logger.error(f"{sym}: не удалось загрузить свечи")
        return

    for _, row in df.iterrows():
        buffers[sym].append(row.to_dict())

    total_initial = len(buffers[sym])
    debug_logger.info(f"{sym}: загружено {total_initial} свечей ({CANDLE_INTERVAL}m)")

    # Однопроходная обработка истории (прогрев искателей)
    block_idx_buffer = []
    block_start = 0
    for i in range(total_initial):
        current_candle = pd.Series(buffers[sym][i])
        block_idx_buffer.append(i)
        if len(block_idx_buffer) == BLOCK_SIZE:
            block_df = pd.DataFrame(
                list(buffers[sym])[block_idx_buffer[0]:block_idx_buffer[-1] + 1],
                index=block_idx_buffer,
            )
            buffer_df = pd.DataFrame(list(buffers[sym]))
            process_block(state, block_start, block_df, params[sym], buffer_df=buffer_df)
            block_idx_buffer = []
            block_start = i + 1

        # прогреваем check_entry, но сигналы на истории игнорируем
        _ = check_entry(state, i, current_candle, params[sym])

    last_processed_idx[sym] = total_initial - 1
    debug_logger.info(f"{sym}: стартовая обработка завершена")

    # 4. Telegram
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg = None
    if tg_token:
        async def upload_logs_callback():
            if yadisk:
                return await upload_logs_to_disk(yadisk)
            return False

        tg = TelegramBot(
            tg_token,
            stop_callback=lambda: set_running(False),
            sync_callback=lambda: yadisk.sync_if_updated() if yadisk else None,
            upload_logs_callback=upload_logs_callback,
            reload_params_callback=reload_params_from_disk,
            upload_params_callback=upload_params_to_disk,
        )
        await tg.start()
        debug_logger.info("Telegram-бот запущен")

    if tg:
        await tg.send_notification(
            f"Live-бот запущен ({CANDLE_INTERVAL}m, BTCUSDT, Demo linear, leverage=1x)"
        )

    # [+] PATCH 5: точка отсчёта для журнала + фоновая задача health-check
    start_ts = int(time.time())
    asyncio.create_task(
        startup_snapshot_and_healthcheck(order_mgr, tg, yadisk, start_ts)
    )
    # [-] PATCH 5 END

    last_sync = time.time()
    last_log_upload = time.time()

    # 5. Главный цикл
    while running:
        try:
            # --- Синхронизация конфига ---
            if yadisk and time.time() - last_sync > 300:
                if yadisk.sync_if_updated():
                    with open(YAML_PATH, "r", encoding="utf-8") as f:
                        new_params = yaml.safe_load(f)
                    params.clear()
                    params.update(new_params)
                    if tg:
                        tg.params = params
                    debug_logger.info("Конфиг обновлён с Я.Диска")
                last_sync = time.time()

            # [+] PATCH 6: раз в час — debug.log + journalctl за прошедший час
            if yadisk and time.time() - last_log_upload > 3600:
                await upload_logs_to_disk(yadisk)
                upload_journal_to_disk(yadisk, int(time.time()) - 3600)
                last_log_upload = time.time()
            # [-] PATCH 6 END

            # --- Проверка открытой позиции ---
            live_pos = load_live_position()
            if live_pos is not None:
                pos_info = order_mgr.get_position(sym)

                # Позиция закрыта биржей (сработал стоп или вручную)
                if pos_info is None:
                    debug_logger.info("Позиция закрыта биржей (стоп или вручную)")
                    if tg:
                        await tg.send_notification(
                            f"🛑 {live_pos['side']} {sym} закрыта биржей"
                        )
                    clear_manual_target()
                    clear_live_position()
                    order_mgr.cancel_all_orders(sym)
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                # Проверка цели
                target = live_pos.get('target_price', 0.0)
                side = live_pos['side']
                if target > 0:
                    last_price = order_mgr.get_last_price(sym)
                    if last_price is not None:
                        target_hit = (
                            (side == 'LONG' and last_price >= target) or
                            (side == 'SHORT' and last_price <= target)
                        )
                        if target_hit:
                            close_id = order_mgr.close_position(sym)
                            if close_id:
                                debug_logger.info(
                                    f"ЗАКРЫТИЕ ПО TARGET @ {last_price:.2f}"
                                )
                                if tg:
                                    await tg.send_notification(
                                        f"🎯 ЦЕЛЬ: {side} {sym} @ {last_price:.2f}"
                                    )
                                clear_manual_target()
                                clear_live_position()
                                order_mgr.cancel_all_orders(sym)

                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # --- Позиции нет — ищем сигнал ---
            candle = await get_current_candle(http_session, sym)
            if candle is None:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            last_ts = buffers[sym][-1]['timestamp'] if buffers[sym] else None
            if last_ts is None or candle['timestamp'] > last_ts:
                buffers[sym].append(candle.to_dict())
                total_candles = len(buffers[sym])
                current_idx = total_candles - 1

                debug_logger.info(
                    f"{sym} НОВАЯ СВЕЧА idx={current_idx}: {candle['timestamp']} "
                    f"O={candle['open']:.2f} H={candle['high']:.2f} "
                    f"L={candle['low']:.2f} C={candle['close']:.2f}"
                )

                while total_candles - 1 - last_processed_idx[sym] >= BLOCK_SIZE:
                    start_idx = last_processed_idx[sym] + 1
                    end_idx = start_idx + BLOCK_SIZE - 1
                    if end_idx >= total_candles:
                        break
                    block_df = pd.DataFrame(
                        list(buffers[sym])[start_idx:end_idx + 1],
                        index=range(start_idx, end_idx + 1),
                    )
                    buffer_df = pd.DataFrame(list(buffers[sym]))
                    process_block(state, start_idx, block_df, params[sym],
                                  buffer_df=buffer_df)
                    last_processed_idx[sym] = end_idx

                current_candle = pd.Series(buffers[sym][current_idx])
                signal = check_entry(state, current_idx, current_candle, params[sym])

                if signal:
                    debug_logger.info(
                        f"{sym}: СИГНАЛ idx={current_idx} тип={signal['type']} "
                        f"цена={signal['entry_price']:.2f}"
                    )

                    entry_price = signal['entry_price']
                    side = signal['type']  # 'LONG' или 'SHORT'

                    # [+] PATCH 2: считаем structural_stop_distance из структуры
                    # LONG: entry − min2, SHORT: max2 − entry
                    if side == 'LONG':
                        structural_stop_distance = entry_price - signal['min2'][1]
                    else:
                        structural_stop_distance = signal['max2'][1] - entry_price

                    # Защита от мусора
                    if structural_stop_distance <= 0:
                        debug_logger.error(
                            f"Некорректная structural_stop_distance="
                            f"{structural_stop_distance} для {side}, "
                            f"entry={entry_price}, fallback на %-стоп"
                        )
                        structural_stop_distance = (
                            entry_price
                            * params[sym]['MAX_STOP_DISTANCE_PERCENT'] / 100
                        )
                    # [-] PATCH 2 END

                    # Стоп с учётом cap
                    max_allowed_stop_dist = (
                        entry_price * params[sym]['MAX_STOP_DISTANCE_PERCENT'] / 100
                    )
                    if structural_stop_distance <= max_allowed_stop_dist:
                        actual_stop_distance = structural_stop_distance
                    else:
                        actual_stop_distance = max_allowed_stop_dist

                    if side == 'LONG':
                        stop_loss = entry_price - actual_stop_distance
                        open_side = 'Buy'
                        close_side = 'Sell'
                    else:
                        stop_loss = entry_price + actual_stop_distance
                        open_side = 'Sell'
                        close_side = 'Buy'

                    # Размер позиции
                    balance_usdt = order_mgr.get_wallet_usdt() or 1000.0
                    qty = params[sym]['MAX_LOSS_PER_TRADE'] / actual_stop_distance
                    position_value = qty * entry_price

                    max_pos = balance_usdt * params[sym]['MAX_POSITION_PERCENT'] / 100
                    if position_value > max_pos:
                        position_value = max_pos
                        qty = position_value / entry_price

                    if position_value < params[sym]['MIN_POSITION_USDT']:
                        debug_logger.warning(
                            f"Маленькая позиция {position_value:.2f}, пропуск"
                        )
                    else:
                        # Целевая цена
                        min1_p = signal['min1'][1]
                        max1_p = signal['max1'][1]
                        if side == 'LONG':
                            min2_p = signal['min2'][1]
                            max2_p = 0.0
                        else:
                            min2_p = 0.0
                            max2_p = signal['max2'][1]

                        manual_target = load_manual_target()
                        min_profit = params[sym].get('MIN_TARGET_PROFIT', 200.0)

                        target_price = 0.0
                        if manual_target > 0:
                            if side == 'LONG' and (manual_target - entry_price) >= min_profit:
                                target_price = manual_target
                            elif side == 'SHORT' and (entry_price - manual_target) >= min_profit:
                                target_price = manual_target
                        else:
                            structural_target = calc_structural_target(
                                side, min1_p, max1_p, min2_p, max2_p
                            )
                            if side == 'LONG' and (structural_target - entry_price) >= min_profit:
                                target_price = structural_target
                            elif side == 'SHORT' and (entry_price - structural_target) >= min_profit:
                                target_price = structural_target

                        # Открываем по рынку
                        order_id = order_mgr.place_market_order(sym, open_side, qty)
                        if order_id:
                            save_live_position({
                                'side': side,
                                'entry_price': entry_price,
                                'qty': qty,
                                'stop_loss': stop_loss,
                                'target_price': target_price,
                                'order_id': order_id,
                                'opened_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            })
                            debug_logger.info(
                                f"{side} открыт: {sym} {qty:.6f} @ {entry_price:.2f}, "
                                f"stop={stop_loss:.2f}, target={target_price:.2f}"
                            )

                            stop_order_id = order_mgr.place_stop_market_order(
                                sym, close_side, qty, stop_loss
                            )
                            if stop_order_id is None:
                                debug_logger.error("Стоп-маркет не выставлен!")

                            if tg:
                                target_str = (f"{target_price:.2f}"
                                              if target_price > 0 else "формула не дала цели")
                                await tg.send_notification(
                                    f"🚀 {side} открыт: {sym}\n"
                                    f"Вход: {entry_price:.2f}\n"
                                    f"Стоп: {stop_loss:.2f}\n"
                                    f"Цель: {target_str}\n"
                                    f"Qty: {qty:.6f}"
                                )

                    # Сброс состояний обоих искателей
                    state['long']['state'] = 'WAIT_MIN1'
                    state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
                    state['short']['state'] = 'WAIT_MAX1'
                    state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            debug_logger.error(f"Ошибка в главном цикле: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    # Завершение
    debug_logger.info("Бот остановлен")
    if tg:
        await tg.send_notification("Live-бот остановлен.")
        await tg.stop()
    if yadisk:
        await upload_logs_to_disk(yadisk)


if __name__ == "__main__":
    asyncio.run(main())