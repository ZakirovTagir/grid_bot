"""
main.py
Live-бот на 15m BTCUSDT. Bybit Demo Trading (linear perpetual, leverage=1x).

v11.1:
- [LOGGING] Разделение логов. debug.log — только торговля (свечи, сигналы, [STATE], ордера).
  journal — только системные логи (yadisk, pybit, telegram, ошибки API).
- [LOGGING] LOG_MAX_SIZE увеличен до 20 МБ (запас для 12-часовых выгрузок).
- [LOGGING] Выгрузка логов (debug + journal) 2 раза в день (интервал 12 часов).
- [FIX #6] Синхронизация с swing_finder (MAX_STRUCTURE_MOVE_PCT).
"""
from __future__ import annotations
import sys
import subprocess, os

if sys.version_info < (3, 9):
    lib_dir = os.path.join(os.path.dirname(__file__), 'venv', 'lib',
                           f'python{sys.version_info.major}.{sys.version_info.minor}',
                           'site-packages', 'pybit')
    if os.path.exists(lib_dir):
        subprocess.run(
            f"find {lib_dir} -name '*.py' -exec sed -i 's/defaultdict\\[dict\\]/defaultdict/g' {{}} \\;",
            shell=True, check=False
        )

import asyncio
import logging
import os
import json
import time
import subprocess
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
TELEGRAM_STATE_FILE = "config/telegram_state.json"
SYMBOLS = ['BTCUSDT']

STOP_FAIL_FALLBACK_ENABLED = 0
CLOSE_POSITIONS_ON_STARTUP = 1
CLOSE_POSITIONS_ON_SHUTDOWN = 1
CLOSE_ORPHAN_POSITIONS = 1
RECONCILE_ORPHAN_EVERY_N = 10

PUSH_INTERVAL_SEC = 15 * 60
LOG_UPLOAD_INTERVAL_SEC = 12 * 60 * 60  # 2 раза в день
_push_last_ts = 0.0
_loop_iter = 0

INTERVAL_MS = {
    "1": 60 * 1000, "3": 3 * 60 * 1000, "5": 5 * 60 * 1000,
    "15": 15 * 60 * 1000, "30": 30 * 60 * 1000,
    "60": 60 * 60 * 1000, "120": 120 * 60 * 1000,
    "240": 240 * 60 * 1000, "D": 24 * 60 * 60 * 1000,
}

LOG_FILE = "config/debug.log"
LOG_MAX_SIZE = 20 * 1024 * 1024  # 20 MB
LOG_BACKUP_COUNT = 3

# ---------- [LOGGING] Разделение логов ----------
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 1. Системный логгер (journal / stdout)
system_logger = logging.getLogger()
system_logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
system_logger.addHandler(console_handler)

# 2. Торговый логгер (debug.log)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=LOG_BACKUP_COUNT)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

# Логгер main.py для торговых событий
trade_logger = logging.getLogger("trade")
trade_logger.setLevel(logging.DEBUG)
trade_logger.addHandler(file_handler)
trade_logger.propagate = False  # Не уходит в journal

# Логгеры модулей (swing_finder, order_manager)
for mod_name in ("core.swing_finder", "core.order_manager"):
    mod_logger = logging.getLogger(mod_name)
    mod_logger.addHandler(file_handler)
    mod_logger.propagate = False  # Не уходит в journal

# Подавление шума системных библиотек (оставляем только WARNING и выше)
for noisy in ("telegram", "telegram.ext", "telegram.ext.ExtBot",
              "telegram.ext.Updater", "telegram.ext.Application",
              "httpx", "httpcore", "pybit", "urllib3", "asyncio",
              "yadisk", "utils.yadisk_sync"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------- Вспомогательные ----------
def load_manual_target() -> float:
    if not os.path.exists(MANUAL_TARGETS_FILE):
        return 0.0
    try:
        with open(MANUAL_TARGETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("BTCUSDT", 0.0) or 0.0)
    except Exception:
        return 0.0

def clear_manual_target() -> None:
    try:
        data = {}
        if os.path.exists(MANUAL_TARGETS_FILE):
            with open(MANUAL_TARGETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["BTCUSDT"] = 0.0
        with open(MANUAL_TARGETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        trade_logger.info("Ручная цель BTCUSDT обнулена")
    except Exception as e:
        trade_logger.error(f"clear_manual_target failed: {e}")

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

def _is_push_muted() -> bool:
    if not os.path.exists(TELEGRAM_STATE_FILE):
        return False
    try:
        with open(TELEGRAM_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("position_push_muted", False))
    except Exception:
        return False

def _format_position_push(live_pos: dict, last_price: float) -> str:
    side = live_pos.get("side", "?")
    entry = float(live_pos.get("entry_price", 0))
    qty = float(live_pos.get("qty", 0))
    stop = float(live_pos.get("stop_loss", 0))
    target = float(live_pos.get("target_price", 0))
    src = live_pos.get("target_source", "?")
    stop_ok = live_pos.get("stop_ok", None)

    if side == "LONG":
        pnl = (last_price - entry) * qty
        to_target = (target - last_price) if target > 0 else 0
    else:
        pnl = (entry - last_price) * qty
        to_target = (last_price - target) if target > 0 else 0

    lines = [
        f"⏱ {side} BTCUSDT {qty}",
        f"Entry:  {entry:.2f}",
        f"Now:    {last_price:.2f}",
        f"PnL:    {pnl:+.2f} USDT",
        "",
    ]
    stop_line = f"Стоп:   {stop:.2f}"
    if stop_ok is True:
        stop_line += " ✅"
    elif stop_ok is False:
        stop_line += " ⚠️"
    lines.append(stop_line)
    if target > 0:
        lines.append(f"Цель:   {target:.2f} ({src})")
        lines.append(f"До цели: {to_target:.2f} USDT")
    return "\n".join(lines)

def _format_closed_tg(side: str, sym: str, closed: dict | None, last_price: float, kind: str) -> str:
    if closed is not None:
        exit_price = closed["avg_exit"]
        realized = closed["closed_pnl"]
        avg_entry = closed["avg_entry"]
        qty = closed["qty"]

        if kind == "stop":
            head = f"🛑 СТОП: {side} {sym} @ {exit_price:.2f}"
        elif realized >= 0:
            head = f"🎯 ЗАКРЫТО в ПЛЮС: {side} {sym} @ {exit_price:.2f}"
        else:
            head = f"🔻 ЗАКРЫТО в МИНУС: {side} {sym} @ {exit_price:.2f}"

        return (
            f"{head}\n"
            f"Вход:  {avg_entry:.2f}\n"
            f"Выход: {exit_price:.2f}\n"
            f"Qty:   {qty}\n"
            f"PnL:   {realized:+.2f} USDT"
        )
    if kind == "stop":
        return f"🛑 {side} {sym} закрыта биржей @ {last_price:.2f}"
    return f"🎯 ЦЕЛЬ: {side} {sym} @ {last_price:.2f}"

def calc_structural_target(side: str, min1: float, max1: float, min2: float, max2: float) -> float:
    impulse = max1 - min1
    if impulse <= 0:
        return 0.0
    if side == 'LONG':
        return min2 + impulse / 2.0
    else:
        return max2 - impulse / 2.0

async def fetch_candles(http_session: HTTP, symbol: str, interval: str = CANDLE_INTERVAL, limit: int = HISTORY_LIMIT):
    try:
        end = int(datetime.now().timestamp() * 1000)
        step_ms = INTERVAL_MS.get(interval, 60 * 1000)
        start = end - limit * step_ms
        resp = http_session.get_kline(category="spot", symbol=symbol, interval=interval, start=start, end=end, limit=limit)
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
            trade_logger.error(f"Ошибка свечей {symbol}: {resp}")
            return pd.DataFrame()
    except Exception as e:
        trade_logger.error(f"Исключение свечей {symbol}: {e}")
        return pd.DataFrame()

async def get_current_candle(http_session: HTTP, symbol: str):
    df = await fetch_candles(http_session, symbol, limit=2)
    if df.empty or len(df) < 2:
        return None
    return df.iloc[-2]

def _is_timestamp_line(line: str) -> bool:
    return (len(line) >= 19 and line[4] == '-' and line[7] == '-' and line[10] == ' ')

async def upload_logs_to_disk(yadisk: YaDiskSync, since_ts: float, local_path: str = LOG_FILE, remote_dir: str = "grid_bot/logs/"):
    if not yadisk:
        return False
    tmp_path = None
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remote_path = remote_dir + f"debug_{timestamp}.log"
        tmp_path = os.path.join("config", f"debug_delta_{timestamp}.log")
        cutoff_str = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
        kept = 0
        keep = False
        with open(local_path, "r", encoding="utf-8", errors="replace") as src, open(tmp_path, "w", encoding="utf-8") as dst:
            for line in src:
                if _is_timestamp_line(line):
                    keep = line[:19] >= cutoff_str
                if keep:
                    dst.write(line)
                    kept += 1
        if kept == 0:
            system_logger.info("Нет новых строк для выгрузки")
            os.remove(tmp_path)
            return True
        if yadisk.client.exists(remote_path):
            yadisk.client.remove(remote_path)
        success = yadisk.upload_file(tmp_path, remote_path)
        os.remove(tmp_path)
        tmp_path = None
        if success:
            system_logger.info(f"Лог выгружен: {remote_path} ({kept} строк)")
        return success
    except Exception as e:
        system_logger.error(f"Ошибка выгрузки лога: {e}")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def upload_journal_to_disk(yadisk, since_ts: int, remote_dir: str = "grid_bot/logs/"):
    if not yadisk:
        return None
    local_path = None
    try:
        ts_str = datetime.fromtimestamp(since_ts).strftime("%Y%m%d_%H%M%S")
        local_path = f"config/journal_{ts_str}.log"
        remote_path = f"{remote_dir}journal_{ts_str}.log"
        with open(local_path, "w") as f:
            subprocess.run(["journalctl", "-u", "gridbot", "--since", f"@{since_ts}", "--no-pager", "-o", "short-iso"], stdout=f, stderr=subprocess.PIPE, check=False, timeout=30)
        with open(local_path) as f:
            line_count = sum(1 for _ in f)
        if line_count < 10:
            system_logger.info(f"journal короткий ({line_count} строк), расширяю окно на 10 мин")
            with open(local_path, "w") as f:
                subprocess.run(["journalctl", "-u", "gridbot", "--since", f"@{since_ts - 600}", "--no-pager", "-o", "short-iso"], stdout=f, stderr=subprocess.PIPE, check=False, timeout=30)
        if yadisk.upload_file(local_path, remote_path):
            system_logger.info(f"journal выгружен: {remote_path}")
            return remote_path
        system_logger.error("Не удалось выгрузить journal на Я.Диск")
        return None
    except Exception as e:
        system_logger.error(f"upload_journal failed: {e}")
        return None
    finally:
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass

async def startup_snapshot_and_healthcheck(order_mgr, tg, yadisk, start_ts):
    await asyncio.sleep(90)
    lines = []
    balance = order_mgr.get_wallet_usdt()
    if balance is not None:
        lines.append(f"OK Bybit API (баланс {balance:.2f} USDT)")
    else:
        lines.append("FAIL Bybit API (нет ответа)")
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
    remote_path = upload_journal_to_disk(yadisk, start_ts)
    if remote_path:
        lines.append("OK journal (выгружен)")
    else:
        lines.append("FAIL journal (см. debug.log)")
    if tg is not None:
        try:
            text = "Стартовый снимок:\n" + "\n".join(lines)
            if remote_path:
                text += f"\n\nЛог: {remote_path}"
            await tg.send_notification(text)
        except Exception as e:
            system_logger.error(f"startup healthcheck: TG send failed: {e}")

running = True

def set_running(value: bool):
    global running
    running = value

async def main():
    global _push_last_ts, _loop_iter
    start_ts = int(time.time())

    yadisk_token = os.getenv("YADISK_TOKEN")
    yadisk = YaDiskSync(yadisk_token, YAML_PATH, remote_path="grid_bot/config/pairs.yaml") if yadisk_token else None
    if yadisk:
        if not yadisk.client.exists(yadisk.remote_path):
            yadisk.upload()
        else:
            yadisk.download()
            system_logger.info("Конфиг синхронизирован с Яндекс.Диском")

    try:
        with open(YAML_PATH, "r", encoding="utf-8") as f:
            params = yaml.safe_load(f)
    except FileNotFoundError:
        system_logger.error("config/pairs.yaml не найден")
        return

    if 'BTCUSDT' not in params:
        system_logger.error("BTCUSDT не найден в pairs.yaml")
        return

    def reload_params_from_disk():
        try:
            with open(YAML_PATH, "r", encoding="utf-8") as f:
                new_params = yaml.safe_load(f)
            params.clear()
            params.update(new_params)
            system_logger.info("Параметры перечитаны из pairs.yaml")
        except Exception as e:
            system_logger.error(f"reload_params_from_disk failed: {e}")
            raise

    def upload_params_to_disk():
        if not yadisk:
            return
        try:
            if yadisk.upload():
                system_logger.info("pairs.yaml загружен на Яндекс.Диск")
        except Exception as e:
            system_logger.error(f"upload_params_to_disk failed: {e}")
            raise

    http_session = HTTP(testnet=False)
    order_mgr = OrderManager()
    risk_mgr = RiskManager(MAX_TOTAL_RISK_PERCENT)
    sym = 'BTCUSDT'
    order_mgr.set_leverage(sym, leverage=1)

    taker_fee = order_mgr.get_taker_fee(sym)
    if not taker_fee or taker_fee <= 0:
        system_logger.warning(f"taker_fee вернул {taker_fee}, использую fallback 0.00055")
        taker_fee = 0.00055
    system_logger.info(f"taker_fee для {sym}: {taker_fee}")

    if CLOSE_POSITIONS_ON_STARTUP:
        try:
            stale = order_mgr.get_position(sym)
            if stale is not None:
                system_logger.warning(f"При старте найдена открытая позиция: {stale['side']} {stale['qty']} @ {stale['entry_price']}, закрываю по рынку")
                order_mgr.cancel_all_orders(sym)
                closed_list = order_mgr.close_all_positions()
                for c in closed_list:
                    system_logger.info(f"  закрыто при старте: {c}")
                clear_live_position()
                clear_manual_target()
            else:
                system_logger.info("При старте открытых позиций нет")
        except Exception as e:
            system_logger.error(f"startup reconciliation failed: {e}", exc_info=True)

    state = {
        'long': {'state': 'WAIT_MIN1', 'min1': None, 'max1': None, 'min2': None},
        'short': {'state': 'WAIT_MAX1', 'max1': None, 'min1': None, 'max2': None}
    }

    buffers = {sym: []}
    last_processed_idx = {sym: -1}

    df = await fetch_candles(http_session, sym)
    if df.empty:
        system_logger.error(f"{sym}: не удалось загрузить свечи")
        return
    for _, row in df.iterrows():
        buffers[sym].append(row.to_dict())
    total_initial = len(buffers[sym])
    trade_logger.info(f"{sym}: загружено {total_initial} свечей ({CANDLE_INTERVAL}m)")

    block_idx_buffer = []
    block_start = 0
    for i in range(total_initial):
        current_candle = pd.Series(buffers[sym][i])
        block_idx_buffer.append(i)
        if len(block_idx_buffer) == BLOCK_SIZE:
            block_df = pd.DataFrame(list(buffers[sym])[block_idx_buffer[0]:block_idx_buffer[-1] + 1], index=block_idx_buffer)
            buffer_df = pd.DataFrame(list(buffers[sym]))
            process_block(state, block_start, block_df, params[sym], buffer_df=buffer_df)
            block_idx_buffer = []
            block_start = i + 1
        _ = check_entry(state, i, current_candle, params[sym])
    last_processed_idx[sym] = total_initial - 1
    trade_logger.info(f"{sym}: стартовая обработка завершена")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg = None
    if tg_token:
        async def upload_logs_callback():
            if yadisk:
                return await upload_logs_to_disk(yadisk, since_ts=0.0)
            return False

        tg = TelegramBot(
            tg_token,
            stop_callback=lambda: set_running(False),
            sync_callback=lambda: yadisk.sync_if_updated() if yadisk else None,
            upload_logs_callback=upload_logs_callback,
            reload_params_callback=reload_params_from_disk,
            upload_params_callback=upload_params_to_disk,
            order_mgr=order_mgr,
        )
        await tg.start()
        system_logger.info("Telegram-бот запущен")

    if tg:
        await tg.send_notification(f"Live-бот запущен ({CANDLE_INTERVAL}m, BTCUSDT, Demo linear, leverage=1x)\ntaker_fee: {taker_fee}")

    asyncio.create_task(startup_snapshot_and_healthcheck(order_mgr, tg, yadisk, start_ts))

    last_sync = time.time()
    last_log_upload = time.time()

    while running:
        try:
            _loop_iter += 1

            if yadisk and time.time() - last_sync > 300:
                if yadisk.sync_if_updated():
                    with open(YAML_PATH, "r", encoding="utf-8") as f:
                        new_params = yaml.safe_load(f)
                    params.clear()
                    params.update(new_params)
                    if tg:
                        tg.params = params
                    system_logger.info("Конфиг обновлён с Я.Диска")
                last_sync = time.time()

            if yadisk and time.time() - last_log_upload > LOG_UPLOAD_INTERVAL_SEC:
                await upload_logs_to_disk(yadisk, since_ts=last_log_upload)
                upload_journal_to_disk(yadisk, int(time.time()) - LOG_UPLOAD_INTERVAL_SEC)
                last_log_upload = time.time()

            live_pos = load_live_position()
            if live_pos is not None:
                pos_info = order_mgr.get_position(sym)

                if pos_info is None:
                    trade_logger.info("Позиция закрыта биржей (стоп или вручную)")
                    closed = order_mgr.get_closed_pnl(sym)
                    if tg:
                        text = _format_closed_tg(live_pos.get('side', '?'), sym, closed, order_mgr.get_last_price(sym) or 0.0, kind='stop')
                        await tg.send_notification(text)
                    clear_manual_target()
                    clear_live_position()
                    order_mgr.cancel_all_orders(sym)
                    _push_last_ts = 0.0
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                if live_pos.get('stop_ok') is False:
                    retry_stop = live_pos.get('stop_loss', 0.0)
                    if retry_stop > 0:
                        close_side_retry = 'Buy' if live_pos['side'] == 'SHORT' else 'Sell'
                        trade_logger.warning(f"stop_ok=False — повторная попытка выставить стоп @{retry_stop}")
                        retry_id = order_mgr.place_stop_market_order(sym, close_side_retry, pos_info['qty'], retry_stop)
                        if retry_id:
                            live_pos['stop_ok'] = True
                            live_pos['stop_order_id'] = retry_id
                            save_live_position(live_pos)
                            trade_logger.info(f"Стоп перевыставлен: {retry_id}")
                            if tg:
                                await tg.send_notification(f"✅ Стоп перевыставлен {sym} @ {retry_stop:.2f}")

                target = live_pos.get('target_price', 0.0)
                side = live_pos['side']
                if target > 0:
                    last_price = order_mgr.get_last_price(sym)
                    if last_price is not None:
                        target_hit = ((side == 'LONG' and last_price >= target) or (side == 'SHORT' and last_price <= target))
                        if target_hit:
                            close_id = order_mgr.close_position(sym)
                            if close_id:
                                trade_logger.info(f"ЗАКРЫТИЕ ПО TARGET @ {last_price:.2f}")
                                await asyncio.sleep(1.5)
                                closed = order_mgr.get_closed_pnl(sym)
                                if tg:
                                    text = _format_closed_tg(side, sym, closed, last_price, kind='target')
                                    await tg.send_notification(text)
                                clear_manual_target()
                                clear_live_position()
                                order_mgr.cancel_all_orders(sym)
                                _push_last_ts = 0.0
                                await asyncio.sleep(CHECK_INTERVAL)
                                continue

                if tg and not _is_push_muted():
                    now = time.time()
                    if now - _push_last_ts >= PUSH_INTERVAL_SEC:
                        cur = order_mgr.get_last_price(sym)
                        if cur is not None:
                            try:
                                await tg.send_notification(_format_position_push(live_pos, cur))
                                _push_last_ts = now
                            except Exception as e:
                                system_logger.error(f"push failed: {e}")

                await asyncio.sleep(CHECK_INTERVAL)
                continue

            if _loop_iter % RECONCILE_ORPHAN_EVERY_N == 0:
                try:
                    orphan = order_mgr.get_position(sym)
                    if orphan is not None:
                        trade_logger.warning(f"Бесхозная позиция: {orphan['side']} {orphan['qty']} @ {orphan['entry_price']}, файла live_position.json нет")
                        if CLOSE_ORPHAN_POSITIONS:
                            order_mgr.cancel_all_orders(sym)
                            order_mgr.close_position(sym)
                            if tg:
                                await tg.send_notification(f"⚠️ Обнаружена бесхозная позиция {sym}: {orphan['side']} {orphan['qty']}, закрыта по рынку")
                        else:
                            if tg:
                                await tg.send_notification(f"⚠️ Бесхозная позиция {sym}: {orphan['side']} {orphan['qty']} @ {orphan['entry_price']}. Не закрываю (флаг=0).")
                except Exception as e:
                    system_logger.error(f"orphan reconcile failed: {e}")

            candle = await get_current_candle(http_session, sym)
            if candle is None:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            last_ts = buffers[sym][-1]['timestamp'] if buffers[sym] else None
            if last_ts is None or candle['timestamp'] > last_ts:
                buffers[sym].append(candle.to_dict())
                total_candles = len(buffers[sym])
                current_idx = total_candles - 1

                trade_logger.info(f"{sym} НОВАЯ СВЕЧА idx={current_idx}: {candle['timestamp']} O={candle['open']:.2f} H={candle['high']:.2f} L={candle['low']:.2f} C={candle['close']:.2f}")

                while total_candles - 1 - last_processed_idx[sym] >= BLOCK_SIZE:
                    start_idx = last_processed_idx[sym] + 1
                    end_idx = start_idx + BLOCK_SIZE - 1
                    if end_idx >= total_candles:
                        break
                    block_df = pd.DataFrame(list(buffers[sym])[start_idx:end_idx + 1], index=range(start_idx, end_idx + 1))
                    buffer_df = pd.DataFrame(list(buffers[sym]))
                    process_block(state, start_idx, block_df, params[sym], buffer_df=buffer_df)
                    last_processed_idx[sym] = end_idx

                current_candle = pd.Series(buffers[sym][current_idx])
                signal = check_entry(state, current_idx, current_candle, params[sym])

                if signal:
                    trade_logger.info(f"{sym}: СИГНАЛ idx={current_idx} тип={signal['type']} цена={signal['entry_price']:.2f}")
                    signal_entry = signal['entry_price']
                    side = signal['type']

                    balance_usdt = order_mgr.get_wallet_usdt() or 1000.0
                    position_value = balance_usdt * params[sym]['MAX_POSITION_PERCENT'] / 100
                    max_loss_usdt = position_value * params[sym]['MAX_STOP_DISTANCE_PERCENT'] / 100
                    params[sym]['MAX_LOSS_PER_TRADE'] = round(max_loss_usdt, 2)
                    commissions = position_value * taker_fee * 2
                    price_loss = max_loss_usdt - commissions - 10.0

                    trade_logger.info(f"[SIZING] balance={balance_usdt:.2f} position={position_value:.2f} max_loss={max_loss_usdt:.2f} commissions={commissions:.2f} price_loss={price_loss:.2f}")

                    if price_loss <= 0:
                        trade_logger.error(f"price_loss={price_loss:.2f} <= 0, сигнал пропущен")
                    else:
                        qty = position_value / signal_entry
                        stop_distance = price_loss / qty

                        if side == 'LONG':
                            stop_loss = signal_entry - stop_distance
                            open_side = 'Buy'
                            close_side = 'Sell'
                        else:
                            stop_loss = signal_entry + stop_distance
                            open_side = 'Sell'
                            close_side = 'Buy'

                        if position_value < params[sym]['MIN_POSITION_USDT']:
                            trade_logger.warning(f"Маленькая позиция {position_value:.2f}, пропуск")
                        else:
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

                            if manual_target > 0:
                                candidate = manual_target
                                target_source = 'manual'
                            else:
                                candidate = calc_structural_target(side, min1_p, max1_p, min2_p, max2_p)
                                target_source = 'formula'

                            if side == 'LONG':
                                floor_price = signal_entry + min_profit
                                target_price = (max(candidate, floor_price) if candidate > 0 else floor_price)
                            else:
                                ceiling_price = signal_entry - min_profit
                                target_price = (min(candidate, ceiling_price) if candidate > 0 else ceiling_price)

                            qty = order_mgr.round_qty(sym, qty)
                            if qty <= 0:
                                trade_logger.error("qty округлилось до 0, пропуск сигнала")
                            else:
                                order_id = order_mgr.place_market_order(sym, open_side, qty)
                                if not order_id:
                                    trade_logger.error("Market-ордер не прошёл, сделка не открыта")
                                else:
                                    actual_pos = order_mgr.get_position(sym)
                                    if actual_pos and actual_pos.get('entry_price', 0) > 0:
                                        actual_entry = actual_pos['entry_price']
                                        if abs(actual_entry - signal_entry) > 1e-6:
                                            trade_logger.info(f"entry уточнена с биржи: {signal_entry:.2f} → {actual_entry:.2f}")
                                        entry_price = actual_entry
                                        if side == 'LONG':
                                            stop_loss = entry_price - stop_distance
                                        else:
                                            stop_loss = entry_price + stop_distance

                                        if side == 'LONG':
                                            floor_price = entry_price + min_profit
                                            target_price = (max(candidate, floor_price) if candidate > 0 else floor_price)
                                        else:
                                            ceiling_price = entry_price - min_profit
                                            target_price = (min(candidate, ceiling_price) if candidate > 0 else ceiling_price)
                                    else:
                                        entry_price = signal_entry

                                    stop_order_id = order_mgr.place_stop_market_order(sym, close_side, qty, stop_loss)
                                    stop_ok = stop_order_id is not None

                                    if not stop_ok:
                                        trade_logger.error("Стоп-маркет не выставлен!")
                                        if STOP_FAIL_FALLBACK_ENABLED:
                                            trade_logger.warning("Fallback активен: закрываю позицию по рынку")
                                            order_mgr.close_position(sym)
                                            order_mgr.cancel_all_orders(sym)
                                            clear_live_position()
                                            clear_manual_target()
                                            if tg:
                                                await tg.send_notification(f"⚠️ Стоп не встал, позиция закрыта: {sym}")
                                        else:
                                            save_live_position({
                                                'side': side, 'entry_price': entry_price, 'qty': qty,
                                                'stop_loss': stop_loss, 'target_price': target_price, 'target_source': target_source,
                                                'stop_ok': False, 'stop_order_id': None, 'order_id': order_id,
                                                'opened_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            })
                                            trade_logger.warning("Позиция сохранена БЕЗ стопа (stop_ok=False)")
                                            if tg:
                                                await tg.send_notification(f"⚠️ {side} открыт БЕЗ стопа: {sym}\nВход: {entry_price:.2f}\nЦель: {target_price:.2f} ({target_source})\nQty: {qty:.6f}")
                                    else:
                                        save_live_position({
                                            'side': side, 'entry_price': entry_price, 'qty': qty,
                                            'stop_loss': stop_loss, 'target_price': target_price, 'target_source': target_source,
                                            'stop_ok': True, 'stop_order_id': stop_order_id, 'order_id': order_id,
                                            'opened_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        })
                                        _push_last_ts = time.time()
                                        trade_logger.info(f"{side} открыт: {sym} {qty:.6f} @ {entry_price:.2f}, stop={stop_loss:.2f}, target={target_price:.2f} ({target_source}), stop_ok=True")
                                        if tg:
                                            await tg.send_notification(f"🚀 {side} открыт: {sym}\nВход: {entry_price:.2f}\nСтоп: {stop_loss:.2f} ✅\nЦель: {target_price:.2f} ({target_source})\nQty: {qty:.6f}")

                    state['long']['state'] = 'WAIT_MIN1'
                    state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
                    state['short']['state'] = 'WAIT_MAX1'
                    state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            trade_logger.error(f"Ошибка в главном цикле: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    system_logger.info("Бот остановлен")

    if CLOSE_POSITIONS_ON_SHUTDOWN:
        try:
            closed_list = order_mgr.close_all_positions()
            if closed_list:
                system_logger.info(f"При остановке закрыто позиций: {len(closed_list)}")
                for c in closed_list:
                    system_logger.info(f"  shutdown close: {c}")
                clear_live_position()
                clear_manual_target()
                if tg:
                    lines = [f"⏹ Бот остановлен, закрыто позиций: {len(closed_list)}"]
                    for c in closed_list:
                        ok = "✅" if c['success'] else "❌"
                        lines.append(f"  {ok} {c['symbol']} {c['side']} size={c['size']}")
                    await tg.send_notification("\n".join(lines))
        except Exception as e:
            system_logger.error(f"shutdown close failed: {e}", exc_info=True)

    if tg:
        await tg.send_notification("Live-бот остановлен.")
        await tg.stop()
    if yadisk:
        await upload_logs_to_disk(yadisk, since_ts=last_log_upload)

if __name__ == "__main__":
    asyncio.run(main())