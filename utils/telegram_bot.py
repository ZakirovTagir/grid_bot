"""
utils/telegram_bot.py
Telegram-бот для управления целями, оптимизатором и остановки.

v3:
- [PATCH 20] /close_all confirm — закрытие всех позиций по рынку.
  Без confirm — только предупреждение.

v2:
- [PATCH 3] /target применяется к ТЕКУЩЕЙ сделке.
- [PATCH 17] /close — рыночное закрытие.
- [PATCH 19] /show_target — показывает к текущей или следующей.
- [PATCH 27] /position.
- [PATCH 29/30] /mute_position + /unmute_position.
"""
import os
import sys
import logging
import asyncio
import subprocess
import shutil
import json
import yaml
from datetime import datetime
from telegram import Update
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)

YAML_PATH = "config/pairs.yaml"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPTIMIZATION_DIR = os.path.join(BASE_DIR, "optimization")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
STATE_FILE = os.path.join(OPTIMIZATION_DIR, "current_tf.txt")
LOCK_FILE = os.path.join(OPTIMIZATION_DIR, "optimize.lock")
RESULTS_DIR = os.path.join(OPTIMIZATION_DIR, "results")
OPTIMIZE_SCRIPT = os.path.join(BASE_DIR, "optimize.py")
OPTIMIZE_LOG = os.path.join(OPTIMIZATION_DIR, "optimize_launch.log")
MANUAL_TARGETS_FILE = os.path.join(CONFIG_DIR, "manual_targets.json")
LIVE_POSITION_FILE = os.path.join(CONFIG_DIR, "live_position.json")
TELEGRAM_STATE_FILE = os.path.join(CONFIG_DIR, "telegram_state.json")

ALLOWED_TF = {"60", "30", "15"}
TF_LABEL = {"60": "1h", "30": "30m", "15": "15m"}


class TelegramBot:
    def __init__(self, token: str, stop_callback=None, sync_callback=None,
                 upload_logs_callback=None, reload_params_callback=None,
                 upload_params_callback=None, order_mgr=None):
        self.token = token
        self.chat_id = int(os.getenv("TELEGRAM_CHAT_ID", 0))
        self.stop_callback = stop_callback
        self.sync_callback = sync_callback
        self.upload_logs_callback = upload_logs_callback
        self.reload_params_callback = reload_params_callback
        self.upload_params_callback = upload_params_callback
        self.order_mgr = order_mgr

        self.app = Application.builder().token(token).build()

        self.app.add_handler(CommandHandler("set_target", self.set_target))
        self.app.add_handler(CommandHandler("stop_bot", self.stop_bot))
        self.app.add_handler(CommandHandler("sync", self.sync_config))
        self.app.add_handler(CommandHandler("upload_logs", self.upload_logs))
        self.app.add_handler(CommandHandler("test_1h", self.test_1h))
        self.app.add_handler(CommandHandler("test_30m", self.test_30m))
        self.app.add_handler(CommandHandler("test_15m", self.test_15m))
        self.app.add_handler(CommandHandler("opt_status", self.opt_status))
        self.app.add_handler(CommandHandler("apply_candidate", self.apply_candidate))
        self.app.add_handler(CommandHandler("target", self.set_manual_target))
        self.app.add_handler(CommandHandler("clear_target", self.clear_manual_target))
        self.app.add_handler(CommandHandler("show_target", self.show_manual_target))
        self.app.add_handler(CommandHandler("close", self.close_position_cmd))
        self.app.add_handler(CommandHandler("close_all", self.close_all_positions_cmd))
        self.app.add_handler(CommandHandler("position", self.show_position))
        self.app.add_handler(CommandHandler("mute_position", self.mute_position))
        self.app.add_handler(CommandHandler("unmute_position", self.unmute_position))

        self.params = {}

    # ------------------------------------------------------------
    #  Жизненный цикл
    # ------------------------------------------------------------
    async def _clear_webhook(self):
        try:
            info = await self.app.bot.get_webhook_info()
            if info.url:
                logger.warning(f"Webhook найден: {info.url}, удаляю")
                await self.app.bot.delete_webhook(drop_pending_updates=False)
        except TelegramError as e:
            logger.error(f"webhook check failed: {e}")

    async def start(self, max_retries: int = 5, retry_delay: int = 10):
        await self.app.initialize()
        await self.app.start()
        for attempt in range(1, max_retries + 1):
            try:
                await self._clear_webhook()
                await self.app.updater.start_polling()
                logger.info("Telegram-бот запущен (polling)")
                return
            except Conflict as e:
                logger.warning(f"Conflict (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)
                else:
                    raise

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    # ------------------------------------------------------------
    #  Утилиты
    # ------------------------------------------------------------
    def load_params(self):
        try:
            with open(YAML_PATH, "r", encoding="utf-8") as f:
                self.params = yaml.safe_load(f)
        except FileNotFoundError:
            self.params = {}

    def save_params(self):
        with open(YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(self.params, f, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _read_json(path, default=None):
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    @staticmethod
    def _write_json(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _read_live_position(self):
        return self._read_json(LIVE_POSITION_FILE, None)

    def _write_live_position(self, data):
        self._write_json(LIVE_POSITION_FILE, data)

    def _clear_live_position(self):
        if os.path.exists(LIVE_POSITION_FILE):
            try:
                os.remove(LIVE_POSITION_FILE)
            except Exception as e:
                logger.error(f"clear live_position failed: {e}")

    def _read_state(self):
        return self._read_json(TELEGRAM_STATE_FILE, {}) or {}

    def _write_state(self, data):
        self._write_json(TELEGRAM_STATE_FILE, data)

    def _read_current_tf(self) -> str:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                tf = f.read().strip()
            if tf in ALLOWED_TF:
                return tf
        return "60"

    def _write_current_tf(self, tf: str) -> None:
        os.makedirs(OPTIMIZATION_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write(tf)

    def _lock_pid(self):
        if not os.path.exists(LOCK_FILE):
            return None
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            return None

    def _latest_calc_report(self, tf: str):
        if not os.path.isdir(RESULTS_DIR):
            return None
        files = [os.path.join(RESULTS_DIR, x) for x in os.listdir(RESULTS_DIR)
                 if x.endswith(f"_{tf}m_calc_report.txt")]
        if not files:
            return None
        files.sort(key=os.path.getmtime, reverse=True)
        return files[0]

    def _launch_optimize(self, tf: str) -> bool:
        os.makedirs(OPTIMIZATION_DIR, exist_ok=True)
        log_file = open(OPTIMIZE_LOG, "a")
        try:
            subprocess.Popen(
                [sys.executable, "-u", OPTIMIZE_SCRIPT, "--tf", tf],
                cwd=BASE_DIR,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            return True
        except Exception as e:
            logger.error(f"launch optimize failed: {e}")
            return False

    # ------------------------------------------------------------
    #  Оригинальные команды
    # ------------------------------------------------------------
    async def set_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            parts = context.args
            if len(parts) != 3:
                await update.message.reply_text(
                    "Формат: /set_target SYMBOL PART PRICE")
                return
            symbol = parts[0].upper()
            part = int(parts[1])
            price = float(parts[2])
            if part not in (1, 2):
                await update.message.reply_text("PART должен быть 1 или 2")
                return
            self.load_params()
            if symbol not in self.params:
                await update.message.reply_text(f"{symbol} не найдена")
                return
            self.params[symbol][f"TRAILING_PRICE{part}"] = price
            self.save_params()
            await update.message.reply_text(
                f"✅ {symbol} TRAILING_PRICE{part} = {price:.2f}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")

    async def stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Останавливаю бота...")
        if self.stop_callback:
            self.stop_callback()

    async def sync_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.sync_callback:
            self.sync_callback()
            await update.message.reply_text("Конфиг синхронизирован")
        else:
            await update.message.reply_text("Яндекс.Диск не настроен")

    async def upload_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.upload_logs_callback:
            await update.message.reply_text("Функция выгрузки логов не настроена.")
            return
        await update.message.reply_text("Начинаю выгрузку логов...")
        try:
            success = await self.upload_logs_callback()
            msg = "✅ Логи выгружены." if success else "❌ Не удалось выгрузить."
            await update.message.reply_text(msg)
        except Exception as e:
            logger.error(f"upload_logs failed: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def _run_calc(self, update: Update, tf: str):
        pid = self._lock_pid()
        if pid is not None:
            await update.message.reply_text(
                f"⚠️ Расчёт уже идёт (PID {pid}).\nПроверь: /opt_status"
            )
            return
        self._write_current_tf(tf)
        if not self._launch_optimize(tf):
            await update.message.reply_text(
                f"❌ Не удалось запустить расчёт для {TF_LABEL[tf]}.\n"
                f"См. лог: {OPTIMIZE_LOG}")
            return
        await update.message.reply_text(
            f"✅ Расчёт запущен: TF={TF_LABEL[tf]}\n\n"
            f"Время: ~30 секунд\n"
            f"По завершении придёт TG-уведомление.\n"
            f"Статус: /opt_status"
        )

    async def test_1h(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._run_calc(update, "60")

    async def test_30m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._run_calc(update, "30")

    async def test_15m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._run_calc(update, "15")

    async def opt_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        tf = self._read_current_tf()
        pid = self._lock_pid()
        lines = [f"Текущий TF: {TF_LABEL[tf]} ({tf})"]
        if pid is not None:
            lines.append(f"Статус: ⏳ расчёт идёт (PID {pid})")
        else:
            lines.append("Статус: свободен")
        report = self._latest_calc_report(tf)
        if report:
            mtime = datetime.fromtimestamp(
                os.path.getmtime(report)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"Последний отчёт: {os.path.basename(report)} ({mtime})")
        else:
            lines.append(f"Отчётов для {TF_LABEL[tf]}: нет")
        cand = os.path.join(CONFIG_DIR, f"pairs_candidate_{tf}m.yaml")
        if os.path.exists(cand):
            mtime = datetime.fromtimestamp(
                os.path.getmtime(cand)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"Кандидат: pairs_candidate_{tf}m.yaml ({mtime})")
        else:
            lines.append("Кандидат: отсутствует")
        await update.message.reply_text("\n".join(lines))

    async def apply_candidate(self, update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
        tf = self._read_current_tf()
        cand_path = os.path.join(CONFIG_DIR, f"pairs_candidate_{tf}m.yaml")
        if not os.path.exists(cand_path):
            await update.message.reply_text(
                f"❌ Нет кандидата для {TF_LABEL[tf]} ({cand_path})")
            return
        backup_path = os.path.join(
            CONFIG_DIR,
            f"pairs_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml")
        try:
            shutil.copy2(YAML_PATH, backup_path)
        except Exception as e:
            await update.message.reply_text(f"❌ Бэкап не создан: {e}")
            return
        try:
            shutil.copy2(cand_path, YAML_PATH)
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось применить: {e}")
            return
        upload_ok = True
        if self.upload_params_callback:
            try:
                self.upload_params_callback()
            except Exception as e:
                logger.error(f"upload params failed: {e}")
                upload_ok = False
        reload_ok = True
        if self.reload_params_callback:
            try:
                self.reload_params_callback()
            except Exception as e:
                logger.error(f"reload failed: {e}")
                reload_ok = False
        try:
            with open(YAML_PATH, "r", encoding="utf-8") as f:
                new_cfg = yaml.safe_load(f) or {}
            btc = new_cfg.get("BTCUSDT", {})
            info = btc.get("_optimization", {})
            lines = [
                f"✅ Применён кандидат для {TF_LABEL[tf]}",
                f"Бэкап: {os.path.basename(backup_path)}",
                f"Загрузка на Я.Диск: {'ок' if upload_ok else 'ошибка'}",
                f"Reload: {'ок' if reload_ok else 'ошибка'}",
                "",
                "Параметры BTCUSDT:",
            ]
            for k in ("N", "MIN_BODY_RATIO", "DELTA_PRICE", "MIN_DISTANCE_BARS",
                      "MAX_DELTA_EXTREMES", "ENTRY_TOLERANCE_USD",
                      "MIN_BARS_AFTER_POINT2", "MAX_BARS_AFTER_POINT2"):
                if k in btc:
                    lines.append(f"  {k} = {btc[k]}")
            if info:
                lines.append("")
                lines.append(f"net_pnl: {info.get('net_pnl')}")
                lines.append(f"trades: {info.get('num_trades')}")
                lines.append(f"win_rate: {info.get('win_rate')}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"⚠️ Применено, но ошибка чтения: {e}")

    # ------------------------------------------------------------
    #  Ручная цель
    # ------------------------------------------------------------
    def _read_manual_targets(self):
        return self._read_json(MANUAL_TARGETS_FILE, {}) or {}

    def _write_manual_targets(self, data):
        self._write_json(MANUAL_TARGETS_FILE, data)

    def _get_min_profit(self, symbol="BTCUSDT") -> float:
        self.load_params()
        btc = self.params.get(symbol, {})
        try:
            return float(btc.get("MIN_TARGET_PROFIT", 200.0))
        except Exception:
            return 200.0

    async def set_manual_target(self, update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
        try:
            args = context.args
            if not args:
                await update.message.reply_text(
                    "Формат: /target <price>\n"
                    "Пример: /target 85000\n"
                    "Сброс: /target 0\n\n"
                    "Текущий статус: /show_target")
                return
            requested = float(args[0])

            if requested <= 0:
                self._write_manual_targets({"BTCUSDT": 0.0})
                live_pos = self._read_live_position()
                if live_pos is not None:
                    live_pos["target_price"] = 0.0
                    live_pos["target_source"] = "formula"
                    self._write_live_position(live_pos)
                await update.message.reply_text(
                    "✅ Ручная цель обнулена.\n"
                    "Управление целью по формуле (MIN_TARGET_PROFIT как floor).")
                return

            min_profit = self._get_min_profit("BTCUSDT")
            live_pos = self._read_live_position()

            if live_pos is not None:
                side = live_pos.get("side")
                entry = float(live_pos.get("entry_price", 0))
                applied = requested
                clamped_msg = ""
                if side == "LONG":
                    floor_price = entry + min_profit
                    if requested < floor_price:
                        applied = floor_price
                        clamped_msg = (
                            f"\n⚠️ Цель была ближе MIN_TARGET_PROFIT, "
                            f"подтянута до {applied:.2f} (floor)."
                        )
                else:
                    ceiling_price = entry - min_profit
                    if requested > ceiling_price:
                        applied = ceiling_price
                        clamped_msg = (
                            f"\n⚠️ Цель была ближе MIN_TARGET_PROFIT, "
                            f"подтянута до {applied:.2f} (floor)."
                        )
                live_pos["target_price"] = applied
                live_pos["target_source"] = "manual"
                self._write_live_position(live_pos)
                self._write_manual_targets({"BTCUSDT": applied})
                await update.message.reply_text(
                    f"✅ Цель применена к ТЕКУЩЕЙ {side} BTCUSDT: {applied:.2f}"
                    f"{clamped_msg}")
            else:
                self._write_manual_targets({"BTCUSDT": requested})
                await update.message.reply_text(
                    f"✅ Цель BTCUSDT = {requested:.2f} сохранена.\n"
                    f"Применится к следующей сделке.\n"
                    f"MIN_TARGET_PROFIT = {min_profit} (floor).")
        except ValueError:
            await update.message.reply_text("❌ Цена должна быть числом.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def clear_manual_target(self, update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
        try:
            self._write_manual_targets({"BTCUSDT": 0.0})
            live_pos = self._read_live_position()
            if live_pos is not None:
                live_pos["target_price"] = 0.0
                live_pos["target_source"] = "formula"
                self._write_live_position(live_pos)
                await update.message.reply_text(
                    "✅ Ручная цель сброшена. Текущая сделка пойдёт по формуле.")
            else:
                await update.message.reply_text(
                    "✅ Ручная цель сброшена. Управление по формуле.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def show_manual_target(self, update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
        try:
            targets = self._read_manual_targets()
            manual_price = float(targets.get("BTCUSDT", 0.0) or 0.0)

            self.load_params()
            btc = self.params.get("BTCUSDT", {})
            min_target_profit = btc.get("MIN_TARGET_PROFIT", "?")
            target_delay = btc.get("TARGET_BARS_DELAY", "?")

            live_pos = self._read_live_position()

            lines = ["BTCUSDT управление целью:"]
            if live_pos is not None:
                side = live_pos.get("side", "?")
                entry = float(live_pos.get("entry_price", 0))
                target = float(live_pos.get("target_price", 0))
                src = live_pos.get("target_source", "?")
                lines.append(f"  Сделка открыта: {side} @ {entry:.2f}")
                lines.append(f"  Цель текущей сделки: {target:.2f} ({src})")
                if manual_price > 0 and src == "manual":
                    lines.append(f"  Ручная цель (активна): {manual_price:.2f}")
                else:
                    lines.append("  Ручная цель: не задана")
            else:
                lines.append("  Позиция: нет")
                if manual_price > 0:
                    lines.append(
                        f"  Ручная цель (на следующую): {manual_price:.2f}")
                else:
                    lines.append("  Ручная цель: не задана")
            lines.append(f"  MIN_TARGET_PROFIT: {min_target_profit} (floor)")
            lines.append(f"  TARGET_BARS_DELAY: {target_delay}")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    # ------------------------------------------------------------
    #  Управление позицией
    # ------------------------------------------------------------
    async def close_position_cmd(self, update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
        if self.order_mgr is None:
            await update.message.reply_text("❌ order_mgr не подключён.")
            return
        live_pos = self._read_live_position()
        if live_pos is None:
            await update.message.reply_text("Нет открытой позиции.")
            return
        await update.message.reply_text("Закрываю позицию по рынку...")
        try:
            order_id = self.order_mgr.close_position("BTCUSDT")
            if order_id:
                self._clear_live_position()
                self._write_manual_targets({"BTCUSDT": 0.0})
                await update.message.reply_text(
                    f"✅ Позиция закрыта по рынку.\norder_id: {order_id}")
            else:
                await update.message.reply_text(
                    "❌ Не удалось закрыть позицию (см. debug.log).")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def close_all_positions_cmd(self, update: Update,
                                      context: ContextTypes.DEFAULT_TYPE):
        """[PATCH 20] /close_all confirm — закрыть все позиции по рынку."""
        args = context.args or []
        if not args or args[0].lower() != "confirm":
            await update.message.reply_text(
                "⚠️ Формат: /close_all confirm\n\n"
                "Закроет ВСЕ открытые позиции по рынку.\n"
                "Действие необратимо. Добавь 'confirm' для подтверждения.")
            return
        if self.order_mgr is None:
            await update.message.reply_text("❌ order_mgr не подключён.")
            return
        await update.message.reply_text("Закрываю все позиции по рынку...")
        try:
            results = self.order_mgr.close_all_positions()
            self._clear_live_position()
            self._write_manual_targets({"BTCUSDT": 0.0})
            if not results:
                await update.message.reply_text("Открытых позиций нет.")
                return
            lines = [f"✅ Обработано позиций: {len(results)}"]
            for r in results:
                ok = "✅" if r.get("success") else "❌"
                lines.append(
                    f"  {ok} {r.get('symbol')} {r.get('side')} "
                    f"size={r.get('size')}"
                )
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def show_position(self, update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
        try:
            live_pos = self._read_live_position()
            if live_pos is None:
                targets = self._read_manual_targets()
                mp = float(targets.get("BTCUSDT", 0.0) or 0.0)
                if mp > 0:
                    await update.message.reply_text(
                        f"Нет открытых позиций.\n"
                        f"Ручная цель на следующую сделку: {mp:.2f}")
                else:
                    await update.message.reply_text(
                        "Нет открытых позиций.\n"
                        "Ручная цель на следующую сделку: не задана")
                return

            side = live_pos.get("side", "?")
            entry = float(live_pos.get("entry_price", 0))
            qty = float(live_pos.get("qty", 0))
            stop = float(live_pos.get("stop_loss", 0))
            target = float(live_pos.get("target_price", 0))
            src = live_pos.get("target_source", "?")
            stop_ok = live_pos.get("stop_ok", None)
            opened_at = live_pos.get("opened_at", "?")

            last_price = None
            pnl = None
            if self.order_mgr is not None:
                last_price = self.order_mgr.get_last_price("BTCUSDT")
                pos_info = self.order_mgr.get_position("BTCUSDT")
                if pos_info is not None:
                    pnl = pos_info.get("unrealized_pnl")

            if last_price is None:
                await update.message.reply_text(
                    f"Не удалось получить цену с биржи.\n"
                    f"Локально: {side} {qty} @ {entry:.2f}\n"
                    f"Стоп: {stop:.2f}\nЦель: {target:.2f} ({src})")
                return

            if pnl is None:
                if side == "LONG":
                    pnl = (last_price - entry) * qty
                else:
                    pnl = (entry - last_price) * qty

            lines = [
                f"📍 BTCUSDT {side} {qty}",
                f"Entry:  {entry:.2f}",
                f"Now:    {last_price:.2f}",
                f"PnL:    {pnl:+.2f} USDT",
                "",
            ]

            stop_line = f"Стоп:   {stop:.2f}"
            if stop_ok is True:
                stop_line += " ✅"
            elif stop_ok is False:
                stop_line += " ⚠️ не выставлен"
            lines.append(stop_line)

            if target > 0:
                to_target = abs(target - last_price)
                lines.append(f"Цель:   {target:.2f} ({src})")
                lines.append(f"До цели: {to_target:.2f} USDT")
            else:
                lines.append("Цель:   не задана")

            lines.append("")
            lines.append(f"Открыта: {opened_at}")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def mute_position(self, update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
        try:
            state = self._read_state()
            state["position_push_muted"] = True
            self._write_state(state)
            await update.message.reply_text(
                "🔇 Push по позиции отключён.\n"
                "Вернуть: /unmute_position")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def unmute_position(self, update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
        try:
            state = self._read_state()
            state["position_push_muted"] = False
            self._write_state(state)
            await update.message.reply_text(
                "🔊 Push по позиции включён (каждые 15 минут).")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    # ------------------------------------------------------------
    #  Уведомления
    # ------------------------------------------------------------
    async def send_notification(self, text: str):
        if self.chat_id:
            try:
                await self.app.bot.send_message(chat_id=self.chat_id, text=text)
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")