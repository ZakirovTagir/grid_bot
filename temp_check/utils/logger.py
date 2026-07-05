import logging
import sys

def setup_logging(level=logging.DEBUG):
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Файл
    file_handler = logging.FileHandler("bot.log", encoding='utf-8')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Убираем дублирование от библиотек
    logging.getLogger("pybit").setLevel(logging.WARNING)

# При импорте модуля сразу настраиваем
setup_logging(level=logging.DEBUG)