"""
utils/telegram_bot.py
Telegram-бот для управления целями, оптимизатором и остановки.

Команды:
    /set_target <SYMBOL> <PART> <PRICE>
    /stop_bot
    /sync
    /upload_logs
    /test_1h        — выбрать TF=1h и запустить расчёт параметров
    /test_30m       — TF=30m + запуск
    /test_15m       — TF=15m + запуск
    /opt_status     — статус калькулятора (TF, lock, последний результат)
    /apply_candidate — применить pairs_candidate_{tf}m.yaml поверх pairs.yaml
"""
import os
import sys
import logging
import asyncio
import subprocess
import shutil
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

ALLOWED_TF = {"60", "30", "15"}
TF_LABEL = {"60": "1h", "30": "30m", "15": "15m"}


class TelegramBot:
    def __init__(self, token: str, stop_callback=None, sync_callback=None,
                 upload_logs_callback=None, reload_params_callback=None):
        self.token = token
        self.chat_id = int(os.getenv("TELEGRAM_CHAT_ID", 0))
        self.stop_callback = stop_callback
        self.sync_callback = sync_callback
        self.upload_logs_callback = upload_logs_callback
        self.reload_params_callback = reload_params_callback

        self.app = Application.builder().token(token).build()

        # основные
        self.app.add_handler(CommandHandler("set_target", self.set_target))
        self.app.add_handler(CommandHandler("stop_bot", self.stop_bot))
        self.app.add_handler(CommandHandler("sync", self.sync_config))
        self.app.add_handler(CommandHandler("upload_logs", self.upload_logs))
        # оптимизатор
        self.app.add_handler(CommandHandler("test_1h", self.test_1h))
        self.app.add_handler(CommandHandler("test_30m", self.test_30m))
        self.app.add_handler(CommandHandler("test_15m", self.test_15m))
        self.app.add_handler(CommandHandler("opt_status", self.opt_status))
        self.app.add_handler(CommandHandler("apply_candidate", self.apply_candidate))

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
        """Запускает optimize.py в фоне как detached процесс."""
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
            await update.message.reply_text(f"✅ {symbol} TRAILING_PRICE{part} = {price:.2f}")
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

    # ------------------------------------------------------------
    #  Запуск расчёта (test_*)
    # ------------------------------------------------------------
    async def _run_calc(self, update: Update, tf: str):
        # 1. Проверка lock
        pid = self._lock_pid()
        if pid is not None:
            await update.message.reply_text(
                f"⚠️ Расчёт уже идёт (PID {pid}).\n"
                f"Проверь статус: /opt_status"
            )
            return

        # 2. Записать TF
        self._write_current_tf(tf)

        # 3. Запустить optimize.py
        if not self._launch_optimize(tf):
            await update.message.reply_text(
                f"❌ Не удалось запустить расчёт для {TF_LABEL[tf]}.\n"
                f"См. лог: {OPTIMIZE_LOG}"
            )
            return

        # 4. Ответ
        await update.message.reply_text(
            f"✅ Расчёт запущен: TF={TF_LABEL[tf]}\n\n"
            f"Время: ~30 секунд (1h), ~30 сек (30m), ~30 сек (15m)\n"
            f"По завершении придёт TG-уведомление.\n"
            f"Статус: /opt_status"
        )

    async def test_1h(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._run_calc(update, "60")

    async def test_30m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._run_calc(update, "30")

    async def test_15m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._run_calc(update, "15")

    # ------------------------------------------------------------
    #  Статус
    # ------------------------------------------------------------
    async def opt_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        tf = self._read_current_tf()
        pid = self._lock_pid()

        lines = [f"Текущий TF: {TF_LABEL[tf]} ({tf})"]

        if pid is not None:
            lines.append(f"Статус: ⏳ расчёт идёт (PID {pid})")
        else:
            lines.append("Статус: свободен")

        # Последний отчёт
        report = self._latest_calc_report(tf)
        if report:
            mtime = datetime.fromtimestamp(os.path.getmtime(report)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"Последний отчёт: {os.path.basename(report)} ({mtime})")
        else:
            lines.append(f"Отчётов для {TF_LABEL[tf]}: нет")

        # Кандидат
        cand = os.path.join(CONFIG_DIR, f"pairs_candidate_{tf}m.yaml")
        if os.path.exists(cand):
            mtime = datetime.fromtimestamp(os.path.getmtime(cand)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"Кандидат: pairs_candidate_{tf}m.yaml ({mtime})")
        else:
            lines.append(f"Кандидат: отсутствует")

        await update.message.reply_text("\n".join(lines))

    # ------------------------------------------------------------
    #  Применение кандидата
    # ------------------------------------------------------------
    async def apply_candidate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        tf = self._read_current_tf()
        cand_path = os.path.join(CONFIG_DIR, f"pairs_candidate_{tf}m.yaml")

        if not os.path.exists(cand_path):
            await update.message.reply_text(
                f"❌ Нет кандидата для {TF_LABEL[tf]} ({cand_path})")
            return

        # Бэкап
        backup_path = os.path.join(
            CONFIG_DIR, f"pairs_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml")
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

        # Sync
        sync_ok = True
        if self.sync_callback:
            try:
                self.sync_callback()
            except Exception as e:
                logger.error(f"sync failed: {e}")
                sync_ok = False

        # Reload
        reload_ok = True
        if self.reload_params_callback:
            try:
                self.reload_params_callback()
            except Exception as e:
                logger.error(f"reload failed: {e}")
                reload_ok = False

        # Показать что применилось
        try:
            with open(YAML_PATH, "r", encoding="utf-8") as f:
                new_cfg = yaml.safe_load(f) or {}
            btc = new_cfg.get("BTCUSDT", {})
            info = btc.get("_optimization", {})
            lines = [
                f"✅ Применён кандидат для {TF_LABEL[tf]}",
                f"Бэкап: {os.path.basename(backup_path)}",
                f"Sync: {'ок' if sync_ok else 'ошибка'}",
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
    #  Уведомления
    # ------------------------------------------------------------
    async def send_notification(self, text: str):
        if self.chat_id:
            try:
                await self.app.bot.send_message(chat_id=self.chat_id, text=text)
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")