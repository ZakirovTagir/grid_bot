"""
trader/config/hot_reloader.py
Модуль для горячей загрузки YAML-конфигов с Яндекс.Диска.
"""
import os
import yaml
import logging
import time
from typing import Dict, Optional
from datetime import datetime
from yadisk import YaDisk

logger = logging.getLogger(__name__)

class HotReloader:
    def __init__(self, token: str, remote_dir: str, local_dir: str, validation_schema: Dict):
        """
        token: OAuth-токен Яндекс.Диска
        remote_dir: путь на Яндекс.Диске (например, 'grid_bot/configs')
        local_dir: локальная папка для сохранения конфигов
        validation_schema: словарь с допустимыми диапазонами для параметров
        """
        self.disk = YaDisk(token=token)
        self.remote_dir = remote_dir
        self.local_dir = local_dir
        self.validation_schema = validation_schema
        os.makedirs(local_dir, exist_ok=True)
        self.last_sync = {}  # файл -> время последнего изменения на диске

    def sync_and_load(self) -> Dict[str, Dict]:
        """
        Проверяет Яндекс.Диск, скачивает новые/изменённые YAML и возвращает словарь конфигов.
        Возвращает {symbol: config_dict}
        """
        if not self.disk.check_token():
            logger.error("Яндекс.Диск: невалидный токен")
            return {}

        if not self.disk.is_dir(self.remote_dir):
            logger.warning(f"Папка {self.remote_dir} не найдена на Яндекс.Диске")
            return {}

        configs = {}
        for file_info in self.disk.listdir(self.remote_dir):
            if not file_info.name.endswith('.yaml') and not file_info.name.endswith('.yml'):
                continue

            remote_path = f"{self.remote_dir}/{file_info.name}"
            local_path = os.path.join(self.local_dir, file_info.name)
            last_modified = file_info.modified

            # Скачиваем, только если файл изменился с последней синхронизации
            if file_info.name in self.last_sync and self.last_sync[file_info.name] >= last_modified:
                # Файл не менялся, загружаем из локальной копии
                if os.path.exists(local_path):
                    config = self._load_yaml(local_path)
                    if config:
                        symbol = file_info.name.split('.')[0]  # BTCUSDT.yaml -> BTCUSDT
                        configs[symbol] = config
                continue

            try:
                self.disk.download(remote_path, local_path)
                logger.info(f"Скачан {file_info.name} с Яндекс.Диска")
                self.last_sync[file_info.name] = last_modified
            except Exception as e:
                logger.error(f"Ошибка скачивания {file_info.name}: {e}")
                continue

            config = self._load_yaml(local_path)
            if config:
                symbol = file_info.name.split('.')[0]
                configs[symbol] = config

        return configs

    def _load_yaml(self, path: str) -> Optional[Dict]:
        """Загружает и валидирует YAML-файл."""
        try:
            with open(path, 'r') as f:
                config = yaml.safe_load(f)
            if self._validate(config):
                return config
            else:
                logger.error(f"Валидация не пройдена для {path}")
                # Переименовываем некорректный файл, чтобы не блокировать работу
                corrupted = path + '.corrupted'
                os.rename(path, corrupted)
                return None
        except Exception as e:
            logger.error(f"Ошибка загрузки YAML {path}: {e}")
            return None

    def _validate(self, config: Dict) -> bool:
        """Проверяет конфиг на соответствие схеме."""
        if not isinstance(config, dict):
            return False
        required_fields = ['strategy', 'params']
        for field in required_fields:
            if field not in config:
                logger.error(f"Отсутствует обязательное поле '{field}'")
                return False
        if config['strategy'] not in ('MaxTrend', 'Grid'):
            logger.error(f"Неизвестная стратегия: {config['strategy']}")
            return False
        # Проверка диапазонов из validation_schema
        for param, rules in self.validation_schema.get(config['strategy'], {}).items():
            if param in config['params']:
                value = config['params'][param]
                if 'min' in rules and value < rules['min']:
                    logger.error(f"Параметр {param}={value} меньше минимума {rules['min']}")
                    return False
                if 'max' in rules and value > rules['max']:
                    logger.error(f"Параметр {param}={value} больше максимума {rules['max']}")
                    return False
        return True