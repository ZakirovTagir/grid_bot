"""
utils/yadisk_sync.py
Синхронизация config/pairs.yaml с Яндекс.Диском.
Добавлен метод для загрузки произвольных файлов (например, логов).
"""
import os
import logging
import yadisk

logger = logging.getLogger(__name__)

class YaDiskSync:
    def __init__(self, token: str, local_path: str, remote_path: str = "grid_bot/config/pairs.yaml"):
        self.token = token
        self.local_path = local_path
        self.remote_path = remote_path
        self.client = yadisk.YaDisk(token=token)

    def upload(self) -> bool:
        """Загружает локальный файл на Яндекс.Диск (по умолчанию pairs.yaml)."""
        try:
            if self.client.exists(self.remote_path):
                self.client.remove(self.remote_path)
            self.client.upload(self.local_path, self.remote_path)
            logger.info("Конфиг загружен на Яндекс.Диск")
            return True
        except Exception as e:
            logger.error(f"Ошибка загрузки конфига на Яндекс.Диск: {e}")
            return False

    def download(self) -> bool:
        """Скачивает файл с Яндекс.Диска, перезаписывая локальный."""
        try:
            if not self.client.exists(self.remote_path):
                logger.warning("Файл на Яндекс.Диске не найден, создаю локальный")
                return False
            self.client.download(self.remote_path, self.local_path)
            logger.info("Конфиг скачан с Яндекс.Диска")
            return True
        except Exception as e:
            logger.error(f"Ошибка скачивания конфига с Яндекс.Диска: {e}")
            return False

    def sync_if_updated(self) -> bool:
        """Скачивает файл, если он изменился на диске (сравнение по дате)."""
        try:
            local_time = os.path.getmtime(self.local_path) if os.path.exists(self.local_path) else 0
            meta = self.client.get_meta(self.remote_path)
            remote_time = meta.modified.timestamp()
            if remote_time > local_time:
                return self.download()
            return False
        except Exception as e:
            logger.error(f"Ошибка проверки обновлений: {e}")
            return False

    # ---------- НОВЫЙ МЕТОД ДЛЯ ЗАГРУЗКИ ПРОИЗВОЛЬНЫХ ФАЙЛОВ ----------
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """
        Загружает любой локальный файл на Яндекс.Диск по указанному удалённому пути.
        При необходимости создаёт родительские папки.
        """
        try:
            # Проверяем, существует ли локальный файл
            if not os.path.exists(local_path):
                logger.error(f"Локальный файл не найден: {local_path}")
                return False

            # Проверяем существование родительской папки на диске и создаём при необходимости
            remote_dir = os.path.dirname(remote_path)
            if remote_dir and not self.client.exists(remote_dir):
                self.client.mkdir(remote_dir)
                logger.debug(f"Создана папка на Яндекс.Диске: {remote_dir}")

            # Если файл уже существует — удаляем (перезаписываем)
            if self.client.exists(remote_path):
                self.client.remove(remote_path)

            # Загружаем файл
            self.client.upload(local_path, remote_path)
            logger.info(f"Файл загружен на Яндекс.Диск: {remote_path}")
            return True
        except Exception as e:
            logger.error(f"Ошибка загрузки файла на Яндекс.Диск: {e}")
            return False