"""
utils/telegram_bot.py
Telegram-бот для управления целями, синхронизации с Яндекс.Диском и аварийной остановки.
Принимает команды:
/set_target <SYMBOL> <PART> <PRICE>
/stop_bot
/sync
"""
import os
import logging
import yaml
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)

YAML_PATH = "config/pairs.yaml"

class TelegramBot:
    def __init__(self, token: str, stop_callback=None, sync_callback=None):
        self.token = token
        self.chat_id = int(os.getenv("TELEGRAM_CHAT_ID", 0))
        self.stop_callback = stop_callback
        self.sync_callback = sync_callback
        self.app = Application.builder().token(token).build()
        self.app.add_handler(CommandHandler("set_target", self.set_target))
        self.app.add_handler(CommandHandler("stop_bot", self.stop_bot))
        self.app.add_handler(CommandHandler("sync", self.sync_config))
        self.params = {}

    async def start(self):
        """Запускает бота в фоне (polling)."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram-бот запущен")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    def load_params(self):
        try:
            with open(YAML_PATH, 'r') as f:
                self.params = yaml.safe_load(f)
        except FileNotFoundError:
            self.params = {}

    def save_params(self):
        with open(YAML_PATH, 'w') as f:
            yaml.dump(self.params, f)

    async def set_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды: /set_target BTCUSDT 1 85000"""
        try:
            parts = context.args
            if len(parts) != 3:
                await update.message.reply_text("Формат: /set_target SYMBOL PART PRICE\nПример: /set_target BTCUSDT 1 85000")
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
            key = f"TRAILING_PRICE{part}"
            self.params[symbol][key] = price
            self.save_params()
            await update.message.reply_text(f"✅ {symbol} TRAILING_PRICE{part} = {price:.2f} USD")
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")

    async def stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Аварийная остановка бота с закрытием всех позиций."""
        await update.message.reply_text("Останавливаю бота и закрываю все позиции...")
        if self.stop_callback:
            self.stop_callback()
        # Здесь можно добавить отправку сообщения о завершении

    async def sync_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Принудительная синхронизация конфига с Яндекс.Диском."""
        if self.sync_callback:
            self.sync_callback()
            await update.message.reply_text("Конфиг синхронизирован")
        else:
            await update.message.reply_text("Яндекс.Диск не настроен")

    async def send_notification(self, text: str):
        """Отправляет сообщение в заданный чат (чат должен быть инициирован)."""
        if self.chat_id:
            try:
                await self.app.bot.send_message(chat_id=self.chat_id, text=text)
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")
        else:
            logger.warning("TELEGRAM_CHAT_ID не задан, уведомление не отправлено")