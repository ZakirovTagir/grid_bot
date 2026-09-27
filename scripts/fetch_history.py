"""
scripts/fetch_history.py
Выгружает 1m-свечи BTCUSDT с Bybit mainnet (публичный API, ключи не нужны)
и сохраняет в CSV в формате бэктестера: timestamp,open,high,low,close,volume,turnover.

Запуск (Windows PowerShell):
    cd C:\\grid_bot\\grid_bot
    python scripts\\fetch_history.py
"""
import csv
import sys
import time
from datetime import datetime, timedelta

from pybit.unified_trading import HTTP

SYMBOL = "BTCUSDT"
INTERVAL = "60"
DAYS_BACK = 30            # сколько дней истории скачать
OUTPUT = "data/historical/BTCUSDT_60.csv"

session = HTTP(testnet=False)  # mainnet — публичные свечи, ключи не нужны


def fetch_window(symbol, interval, start_ms, end_ms):
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


def main():
    end_ms = int(datetime.now().timestamp() * 1000)
    start_ms = int((datetime.now() - timedelta(days=DAYS_BACK)).timestamp() * 1000)

    candles = {}  # ts -> [o, h, l, c, v, turnover], автоматически дедуплицирует
    cursor = start_ms
    while cursor < end_ms:
        window_end = min(cursor + 1000 * 60 * 1000, end_ms)
        batch = fetch_window(SYMBOL, INTERVAL, cursor, window_end)
        for item in batch:
            ts = int(item[0])
            candles[ts] = item[1:7]
        print(f"  окно {datetime.fromtimestamp(cursor/1000)}: +{len(batch)} (итого {len(candles)})")
        cursor = window_end
        time.sleep(0.2)  # бережём rate limit

    # сортируем по времени
    rows = sorted(candles.items())

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume", "turnover"])
        for ts, vals in rows:
            ts_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([ts_str] + vals)

    print(f"\nГотово: {len(rows)} свечей → {OUTPUT}")
    if rows:
        first = datetime.fromtimestamp(rows[0][0] / 1000)
        last = datetime.fromtimestamp(rows[-1][0] / 1000)
        print(f"Диапазон: {first} .. {last}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)