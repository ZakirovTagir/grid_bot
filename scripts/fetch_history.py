"""
scripts/fetch_history.py
Выгружает свечи с Bybit mainnet (публичный API, ключи не нужны)
и сохраняет в CSV в формате бэктестера: timestamp,open,high,low,close,volume,turnover.

Использование:
    # как функция (для оптимизатора)
    from scripts.fetch_history import fetch_history
    n = fetch_history("BTCUSDT", "60", 180, "data/historical/BTCUSDT_60.csv")

    # из командной строки (ручной запуск)
    python scripts/fetch_history.py
"""
import csv
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

from pybit.unified_trading import HTTP


# Дефолты для ручного запуска
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "60"          # "1", "5", "15", "30", "60", "240", "D"
DEFAULT_DAYS_BACK = 180          # 6 месяцев
DEFAULT_OUTPUT = "data/historical/BTCUSDT_60.csv"

# Длительность интервала в миллисекундах (для правильного шага окна)
INTERVAL_MS = {
    "1": 60 * 1000,
    "3": 3 * 60 * 1000,
    "5": 5 * 60 * 1000,
    "15": 15 * 60 * 1000,
    "30": 30 * 60 * 1000,
    "60": 60 * 60 * 1000,
    "120": 120 * 60 * 1000,
    "240": 240 * 60 * 1000,
    "D": 24 * 60 * 60 * 1000,
}


def _fetch_window(session: HTTP, symbol: str, interval: str,
                  start_ms: int, end_ms: int) -> list:
    """Одна пачка свечей (Bybit отдаёт максимум 1000 за раз)."""
    resp = session.get_kline(
        category="spot",
        symbol=symbol,
        interval=interval,
        start=start_ms,
        end=end_ms,
        limit=1000,
    )
    if resp.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {resp}")
    return resp["result"]["list"]  # [[ts, o, h, l, c, volume, turnover], ...]


def fetch_history(symbol: str,
                  interval: str,
                  days_back: int,
                  output_path: str,
                  verbose: bool = True,
                  session: Optional[HTTP] = None) -> int:
    """
    Скачивает свечи с Bybit mainnet и пишет в CSV.
    Возвращает число свечей, записанных в файл.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}. Allowed: {list(INTERVAL_MS.keys())}")

    if session is None:
        session = HTTP(testnet=False)

    interval_ms = INTERVAL_MS[interval]

    end_ms = int(datetime.now().timestamp() * 1000)
    start_ms = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)

    candles = {}  # ts -> [o, h, l, c, v, turnover], дедуплицирует автоматически
    cursor = start_ms
    while cursor < end_ms:
        # Окно = 1000 свечей текущего интервала, но не больше end_ms
        window_end = min(cursor + 1000 * interval_ms, end_ms)
        batch = _fetch_window(session, symbol, interval, cursor, window_end)
        for item in batch:
            ts = int(item[0])
            candles[ts] = item[1:7]
        if verbose:
            print(f"  окно {datetime.fromtimestamp(cursor/1000)}: "
                  f"+{len(batch)} (итого {len(candles)})")
        cursor = window_end
        time.sleep(0.2)  # бережём rate limit

    # сортируем по времени
    rows = sorted(candles.items())

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume", "turnover"])
        for ts, vals in rows:
            ts_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([ts_str] + vals)

    if verbose:
        print(f"\nГотово: {len(rows)} свечей → {output_path}")
        if rows:
            first = datetime.fromtimestamp(rows[0][0] / 1000)
            last = datetime.fromtimestamp(rows[-1][0] / 1000)
            print(f"Диапазон: {first} .. {last}")

    return len(rows)


def main():
    try:
        fetch_history(
            symbol=DEFAULT_SYMBOL,
            interval=DEFAULT_INTERVAL,
            days_back=DEFAULT_DAYS_BACK,
            output_path=DEFAULT_OUTPUT,
            verbose=True,
        )
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()