"""
utils/telegram_bot.py
Telegram-бот для управления целями, синхронизации, оптимизатора и остановки.

Команды:
    /set_target <SYMBOL> <PART> <PRICE>
    /stop_bot
    /sync
    /upload_logs
    /test_1h        — выбрать TF=1h для следующего запуска оптимизатора
    /test_30m       — выбрать TF=30m
    /test_15m       — выбрать TF=15m
    /opt_status     — статус оптимизатора (TF, lock, последний результат)
    /apply_candidate — применить pairs_candidate_{tf}m.yaml поверх pairs.yaml

Патч от 2026-XX-XX:
- deleteWebhook при старте + retry на Conflict;
- новые команды для оптимизатора.
"""
import os
import logging
import asyncio
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
                logger.warning(f"Обнаружен webhook: {info.url} — удаляю")
                await self.app.bot.delete_webhook(drop_pending_updates=False)
                logger.info("Webhook удалён")
            else:
                logger.info("Webhook отсутствует")
        except TelegramError as e:
            logger.error(f"Ошибка проверки webhook: {e}")

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
                    logger.error("Polling не удалось запустить")
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

    def _latest_results_file(self):
        if not os.path.isdir(RESULTS_DIR):
            return None
        files = [os.path.join(RESULTS_DIR, x) for x in os.listdir(RESULTS_DIR)]
        files = [x for x in files if x.endswith(".csv")]
        if not files:
            return None
        files.sort(key=os.path.getmtime, reverse=True)
        return files[0]

    # ------------------------------------------------------------
    #  Оригинальные команды
    # ------------------------------------------------------------
    async def set_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            parts = context.args
            if len(parts) != 3:
                await update.message.reply_text(
                    "Формат: /set_target SYMBOL PART PRICE\nПример: /set_target BTCUSDT 1 85000")
                return
            symbol = parts[0].upper()
            part = int(parts[1])
            price = float(parts[2])
            if part not in (1, 2):
                await update.message.reply_text("PART должен быть 1 или 2")
                return
            self.load_params()
            if symbol not in self.params:
                await update.message.reply_text(f"Пара {symbol} не найдена в конфиге")
                return
            self.params[symbol][f"TRAILING_PRICE{part}"] = price
            self.save_params()
            await update.message.reply_text(f"✅ {symbol} TRAILING_PRICE{part} = {price:.2f} USD")
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")

    async def stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Останавливаю бота и закрываю все позиции...")
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
            if success:
                await update.message.reply_text("✅ Логи выгружены.")
            else:
                await update.message.reply_text("❌ Не удалось выгрузить логи.")
        except Exception as e:
            logger.error(f"Ошибка выгрузки: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    # ------------------------------------------------------------
    #  Выбор TF
    # ------------------------------------------------------------
    async def _set_tf(self, update: Update, tf: str):
        self._write_current_tf(tf)
        await update.message.reply_text(
            f"✅ TF для следующего запуска оптимизатора: {TF_LABEL[tf]}\n"
            f"Файл: {STATE_FILE}\n"
            f"Запуск — по cron в воскресенье 03:00 (SGT)."
        )

    async def test_1h(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._set_tf(update, "60")

    async def test_30m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._set_tf(update, "30")

    async def test_15m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._set_tf(update, "15")

    # ------------------------------------------------------------
    #  Статус оптимизатора
    # ------------------------------------------------------------
    async def opt_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        tf = self._read_current_tf()
        pid = self._lock_pid()
        latest = self._latest_results_file()

        lines = [
            f"Текущий TF: {TF_LABEL[tf]} ({tf})",
            f"State-файл: {os.path.exists(STATE_FILE)}",
            f"Lock-файл: {'PID ' + str(pid) + ' (работает)' if pid else 'свободен'}",
        ]
        if latest:
            mtime = datetime.fromtimestamp(os.path.getmtime(latest)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"Последний результат: {os.path.basename(latest)} ({mtime})")
        else:
            lines.append("Результатов ещё нет")

        cand_path = os.path.join(CONFIG_DIR, f"pairs_candidate_{tf}m.yaml")
        if os.path.exists(cand_path):
            mtime = datetime.fromtimestamp(os.path.getmtime(cand_path)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"Кандидат: {os.path.basename(cand_path)} ({mtime})")
        else:
            lines.append(f"Кандидат для {TF_LABEL[tf]}: отсутствует")

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

        # Бэкап текущего pairs.yaml
        backup_path = os.path.join(CONFIG_DIR,
                                   f"pairs_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml")
        try:
            shutil.copy2(YAML_PATH, backup_path)
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось создать бэкап: {e}")
            return

        # Копирование кандидата поверх
        try:
            shutil.copy2(cand_path, YAML_PATH)
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось применить: {e}")
            return

        # Синхронизация с Яндекс.Диском (если есть callback)
        sync_ok = True
        if self.sync_callback:
            try:
                self.sync_callback()
            except Exception as e:
                logger.error(f"Ошибка sync: {e}")
                sync_ok = False

        # Перечитать params в памяти
        reload_ok = True
        if self.reload_params_callback:
            try:
                self.reload_params_callback()
            except Exception as e:
                logger.error(f"Ошибка reload: {e}")
                reload_ok = False

        # Читаем итоговые параметры, чтобы показать отчёт
        try:
            with open(YAML_PATH, "r", encoding="utf-8") as f:
                new_cfg = yaml.safe_load(f) or {}
            btc = new_cfg.get("BTCUSDT", {})
            info = btc.get("_optimization", {})
            lines = [
                f"✅ Применён кандидат для {TF_LABEL[tf]}",
                f"Бэкап: {os.path.basename(backup_path)}",
                f"Синхронизация с Я.Диск: {'ок' if sync_ok else 'ошибка'}",
                f"Перечитано в память: {'ок' if reload_ok else 'ошибка'}",
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
                lines.append(f"IS net: {info.get('is_net_pnl')} trades: {info.get('is_trades')}")
                lines.append(f"OOS net: {info.get('oos_net_pnl')} trades: {info.get('oos_trades')}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"⚠️ Применено, но не удалось прочитать итог: {e}")

    # ------------------------------------------------------------
    #  Уведомления
    # ------------------------------------------------------------
    async def send_notification(self, text: str):
        if self.chat_id:
            try:
                await self.app.bot.send_message(chat_id=self.chat_id, text=text)
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")
        else:
            logger.warning("TELEGRAM_CHAT_ID не задан")