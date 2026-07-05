"""
optimizer_app/yaml_writer.py
Записывает параметры стратегии в YAML-файл.
"""
import os
import yaml
import logging
from datetime import datetime
from typing import Dict

logger = logging.getLogger(__name__)

def write_yaml(symbol: str, strategy: str, params: Dict, output_dir: str):
    """
    Создаёт YAML-файл для пары.
    """
    os.makedirs(output_dir, exist_ok=True)
    config = {
        'strategy': strategy,
        'last_updated': datetime.now().isoformat(),
        'params': params
    }
    filename = f"{symbol}.yaml"
    path = os.path.join(output_dir, filename)
    with open(path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
    logger.info(f"YAML-конфиг сохранён: {path}")