"""
optimize_v2.py — калькулятор технических параметров A+B с разбивкой по периодам.

Отличия от optimize.py:
- Считает только параметры группы A+B. Группа C полностью исключена.
- Сигналы генерируются через check_entry независимо: LONG и SHORT
  работают параллельно и не блокируют друг друга.
- CSV (90 дней) скачивается при каждом запуске.
- Параметры для сравнения (числа до стрелок) загружаются с Я.Диска,
  чтобы видеть актуальные live-параметры.
- На выходе — четыре блока с параметрами, num_signals, avg_per_day
  + сводная таблица дельт между соседними периодами.
- Константы (N, MIN_BODY_RATIO и т.д.) выведены один раз в шапке отчёта.

v2:
- [FIX #6] Добавлен параметр MAX_STRUCTURE_MOVE_PCT (1.5) в константы
  и в расчет параметров. Влияет на количество сигналов (отбрасывает
  слишком большие структуры).

Запуск:
    py optimize_v2.py --tf 15
    py optimize_v2.py --tf 60
"""
from __future__ import annotations
import os
import sys
import time
import argparse
import traceback
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from find_swing_points import (
    DEFAULT_PARAMS,
    LongFinder,
    ShortFinder,
    collect_block_amplitudes,
    collect_point_distances,
    collect_line_gaps,
)
from scripts.fetch_history import fetch_history

load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------- Пути и константы ----------
OPTIMIZATION_DIR = os.path.join(BASE_DIR, "optimization")
RESULTS_DIR = os.path.join(OPTIMIZATION_DIR, "results")
HISTORICAL_DIR = os.path.join(BASE_DIR, "data", "historical")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
PAIRS_YAML_LOCAL = os.path.join(CONFIG_DIR, "pairs.yaml")
PAIRS_YAML_LIVE = os.path.join(CONFIG_DIR, "pairs_live.yaml")

os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(OPTIMIZATION_DIR, exist_ok=True)

# ---------- Периоды (в днях) ----------
PERIODS = [
    ("90 дней", 90),
    ("30 дней", 30),
    ("7 дней", 7),
    ("3 дня", 3),
]

# ---------- Коэффициенты (как в optimize.py) ----------
DELTA_K = 0.25
TOLERANCE_K = 1.5
TRIM_PCT = 0.10

# ---------- Жёстко захардкоженные ----------
HARD_N = 7
HARD_MIN_BODY_RATIO = 0.3
HARD_MAX_DELTA_EXTREMES = 4.0
HARD_MIN_BARS_AFTER = 3
HARD_MAX_BARS_AFTER = 10
HARD_MAX_STRUCTURE_MOVE_PCT = 1.5

# ---------- Порядок вывода ----------
PARAM_KEYS_ORDER = [
    "N", "MIN_BODY_RATIO", "DELTA_PRICE", "MIN_DISTANCE_BARS",
    "MAX_DELTA_EXTREMES", "ENTRY_TOLERANCE_USD",
    "MIN_BARS_AFTER_POINT2", "MAX_BARS_AFTER_POINT2", "MAX_STRUCTURE_MOVE_PCT",
]

# ---------- Параметры для сводной таблицы и для стрелок в блоках ----------
DYNAMIC_KEYS = ["DELTA_PRICE", "MIN_DISTANCE_BARS", "ENTRY_TOLERANCE_USD"]

# ---------- Константы для шапки отчёта ----------
CONSTANTS = {
    "N": HARD_N,
    "MIN_BODY_RATIO": HARD_MIN_BODY_RATIO,
    "MAX_DELTA_EXTREMES": HARD_MAX_DELTA_EXTREMES,
    "MIN_BARS_AFTER_POINT2": HARD_MIN_BARS_AFTER,
    "MAX_BARS_AFTER_POINT2": HARD_MAX_BARS_AFTER,
    "MAX_STRUCTURE_MOVE_PCT": HARD_MAX_STRUCTURE_MOVE_PCT,
}

# ---------- Я.Диск ----------
YADISK_REMOTE_PAIRS = "grid_bot/config/pairs.yaml"


# ============================================================
#  Утилиты
# ============================================================
def read_current_tf() -> str:
    state_file = os.path.join(OPTIMIZATION_DIR, "current_tf.txt")
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            tf = f.read().strip()
        if tf in {"60", "30", "15"}:
            return tf
    return "60"


def get_csv_path(tf: str) -> str:
    return os.path.join(HISTORICAL_DIR, f"BTCUSDT_{tf}.csv")


def ensure_csv(tf: str, days_back: int) -> str:
    """Всегда скачивает свежий CSV за days_back дней."""
    path = get_csv_path(tf)
    print(f"Скачиваю свежие свечи: TF={tf}, {days_back} дней", flush=True)
    fetch_history("BTCUSDT", tf, days_back, path, verbose=True)
    return path


def load_df(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def slice_by_days(df: pd.DataFrame, days: int) -> pd.DataFrame:
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=days)
    out = df[df["timestamp"] >= start].reset_index(drop=True)
    return out


def trimmed_percentile(values: List[float], pct: float,
                       trim_pct: float = TRIM_PCT) -> float:
    if not values:
        return 0.0
    arr = np.array(values, dtype=float)
    if len(arr) < 5:
        return float(np.percentile(arr, pct * 100))
    lo = np.percentile(arr, trim_pct * 100)
    hi = np.percentile(arr, (1 - trim_pct) * 100)
    trimmed = arr[(arr >= lo) & (arr <= hi)]
    if len(trimmed) == 0:
        trimmed = arr
    return float(np.percentile(trimmed, pct * 100))


def trimmed_median(values: List[float],
                   trim_pct: float = TRIM_PCT) -> float:
    if not values:
        return 0.0
    arr = np.array(values, dtype=float)
    if len(arr) < 5:
        return float(np.median(arr))
    lo = np.percentile(arr, trim_pct * 100)
    hi = np.percentile(arr, (1 - trim_pct) * 100)
    trimmed = arr[(arr >= lo) & (arr <= hi)]
    if len(trimmed) == 0:
        trimmed = arr
    return float(np.median(trimmed))


# ============================================================
#  Загрузка актуальных параметров с Я.Диска
# ============================================================
def download_live_params() -> Optional[Dict[str, Any]]:
    """
    Скачивает pairs.yaml с Я.Диска во временный файл, читает параметры BTCUSDT.
    Возвращает dict или None при ошибке. Локальный pairs.yaml не трогает.
    """
    token = os.getenv("YADISK_TOKEN")
    if not token:
        print("YADISK_TOKEN не задан — параметры live недоступны", flush=True)
        return None

    try:
        from utils.yadisk_sync import YaDiskSync

        if os.path.exists(PAIRS_YAML_LIVE):
            os.remove(PAIRS_YAML_LIVE)

        yd = YaDiskSync(token, PAIRS_YAML_LIVE,
                        remote_path=YADISK_REMOTE_PAIRS)
        ok = yd.download()

        # YaDiskSync может возвращать True/False или None — проверяем файл
        if not os.path.exists(PAIRS_YAML_LIVE):
            print("pairs.yaml не скачался с Я.Диска", flush=True)
            return None

        with open(PAIRS_YAML_LIVE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        params = dict(cfg.get("BTCUSDT", {}))
        params.pop("_optimization", None)
        print("Параметры live загружены с Я.Диска", flush=True)
        return params
    except Exception as e:
        print(f"Ошибка загрузки с Я.Диска: {e}", flush=True)
        print("Использую локальный pairs.yaml", flush=True)
        return None


def load_old_params() -> Dict[str, Any]:
    """
    Пытается загрузить актуальные live-параметры с Я.Диска.
    При неудаче — fallback на локальный pairs.yaml.
    """
    params = download_live_params()
    if params is not None:
        return params

    if os.path.exists(PAIRS_YAML_LOCAL):
        with open(PAIRS_YAML_LOCAL, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        params = dict(cfg.get("BTCUSDT", {}))
        params.pop("_optimization", None)
        print("Параметры взяты из локального pairs.yaml", flush=True)
        return params

    print("pairs.yaml не найден ни на Я.Диске, ни локально", flush=True)
    return {}


# ============================================================
#  Расчёт параметров A+B для одного окна
# ============================================================
def calc_params_for_period(df: pd.DataFrame) -> Dict[str, Any]:
    """Считает A+B для данного df."""
    base = dict(DEFAULT_PARAMS)
    base['N'] = HARD_N
    base['MIN_BODY_RATIO'] = HARD_MIN_BODY_RATIO
    base['MAX_STRUCTURE_MOVE_PCT'] = HARD_MAX_STRUCTURE_MOVE_PCT

    amplitudes = collect_block_amplitudes(df, n=HARD_N)
    delta_price = trimmed_median(amplitudes) * DELTA_K

    distances = collect_point_distances(df, base)
    min_distance = int(round(trimmed_percentile(distances, 0.25)))

    base_tol = dict(base)
    base_tol['MIN_BARS_AFTER_POINT2'] = HARD_MIN_BARS_AFTER
    base_tol['MAX_BARS_AFTER_POINT2'] = HARD_MAX_BARS_AFTER
    gaps = collect_line_gaps(df, base_tol)
    entry_tolerance = trimmed_median(gaps) * TOLERANCE_K

    return {
        'N': HARD_N,
        'MIN_BODY_RATIO': HARD_MIN_BODY_RATIO,
        'DELTA_PRICE': round(float(delta_price), 2),
        'MIN_DISTANCE_BARS': int(min_distance),
        'MAX_DELTA_EXTREMES': HARD_MAX_DELTA_EXTREMES,
        'ENTRY_TOLERANCE_USD': round(float(entry_tolerance), 2),
        'MIN_BARS_AFTER_POINT2': HARD_MIN_BARS_AFTER,
        'MAX_BARS_AFTER_POINT2': HARD_MAX_BARS_AFTER,
        'MAX_STRUCTURE_MOVE_PCT': HARD_MAX_STRUCTURE_MOVE_PCT,
    }


# ============================================================
#  Подсчёт сигналов без группы C
# ============================================================
def count_signals(df: pd.DataFrame, params: Dict[str, Any]) -> Tuple[int, int]:
    """
    Прогон df с двумя finder'ами. check_entry вызывается на каждой свече.
    При сигнале соответствующий finder сбрасывается; второй продолжает
    работу независимо. Возвращает (long_count, short_count).
    """
    N = params['N']
    long_finder = LongFinder(df, params)
    short_finder = ShortFinder(df, params)

    long_count = 0
    short_count = 0

    buffer: List[int] = []
    block_start = 0

    for i in range(len(df)):
        current_candle = df.iloc[i]
        buffer.append(i)

        if len(buffer) == N:
            block = df.iloc[buffer]
            long_finder.process_block(block_start, block)
            short_finder.process_block(block_start, block)
            buffer = []
            block_start = i + 1

        sig_long = long_finder.check_entry(i, current_candle)
        if sig_long is not None:
            long_count += 1
            long_finder.reset()

        sig_short = short_finder.check_entry(i, current_candle)
        if sig_short is not None:
            short_count += 1
            short_finder.reset()

    return long_count, short_count


# ============================================================
#  Формирование отчёта
# ============================================================
def format_period_block(period_name: str,
                        days: int,
                        old_params: Dict[str, Any],
                        new_params: Dict[str, Any],
                        num_signals: int) -> str:
    lines = []
    lines.append(f"=== РАСЧЁТ ЗАВЕРШЁН для периода {period_name} ===")
    for k in DYNAMIC_KEYS:
        old_v = old_params.get(k, '?')
        new_v = new_params.get(k, '?')
        lines.append(f"  {k} = {old_v} → {new_v}")
    avg = num_signals / days if days > 0 else 0.0
    lines.append(f"  num_signals: {num_signals}")
    lines.append(f"  avg_per_day: {avg:.2f}")
    lines.append("")
    return "\n".join(lines)


def format_constants_line() -> str:
    parts = [f"{k}={v}" for k, v in CONSTANTS.items()]
    return "Константы: " + ", ".join(parts)


def format_summary_table(results: List[Dict[str, Any]]) -> str:
    """Сводная таблица: дельта (по модулю) между соседними периодами."""
    delta_headers = ["90д-30д", "30д-7д", "7д-3д"]

    lines = []
    lines.append("--- СВОДНАЯ ТАБЛИЦА (дельта рассчитанных параметров, |разница|) ---")

    name_w = 22
    col_w = 12

    header = (f"{'Параметр':<{name_w}}| "
              + "| ".join(f"{h:<{col_w}}" for h in delta_headers)
              + "|")
    lines.append(header)
    lines.append("-" * len(header))

    def _fmt(val: float, is_float: bool) -> str:
        if is_float:
            return f"{val:.2f}"
        return f"{int(round(val))}"

    for k in DYNAMIC_KEYS:
        v = [r["params"][k] for r in results]
        is_float = isinstance(v[0], float)
        d1 = abs(v[0] - v[1])
        d2 = abs(v[1] - v[2])
        d3 = abs(v[2] - v[3])
        row = (f"{k:<{name_w}}| "
               + f"{_fmt(d1, is_float):<{col_w}}| "
               + f"{_fmt(d2, is_float):<{col_w}}| "
               + f"{_fmt(d3, is_float):<{col_w}}|")
        lines.append(row)

    return "\n".join(lines)


# ============================================================
#  Основной цикл
# ============================================================
def run_all_periods(tf: str) -> str:
    print(f"=== optimize_v2: TF {tf}m, периоды {[p[0] for p in PERIODS]} ===",
          flush=True)

    # 1. Свежий CSV
    csv_path = ensure_csv(tf, max(d for _, d in PERIODS))
    df_full = load_df(csv_path)
    print(f"Загружено {len(df_full)} свечей: {df_full['timestamp'].min()} -> "
          f"{df_full['timestamp'].max()}", flush=True)

    # 2. Live-параметры с Я.Диска (или fallback на локальный pairs.yaml)
    old_params = load_old_params()

    # 3. Прогон по периодам
    report_lines = []
    report_lines.append("=" * 60)
    report_lines.append(f"optimize_v2 — отчёт [{tf}m]")
    report_lines.append(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append(f"CSV: {csv_path}")
    report_lines.append(format_constants_line())
    report_lines.append("=" * 60)
    report_lines.append("")

    period_results: List[Dict[str, Any]] = []

    for period_name, days in PERIODS:
        df_period = slice_by_days(df_full, days)
        print(f"\n[CALC] период {period_name}: {len(df_period)} свечей", flush=True)

        t0 = time.time()
        new_params = calc_params_for_period(df_period)
        long_c, short_c = count_signals(df_period, new_params)
        num_signals = long_c + short_c
        avg = num_signals / days if days > 0 else 0.0
        elapsed = time.time() - t0

        print(f"  DELTA_PRICE = {new_params['DELTA_PRICE']}", flush=True)
        print(f"  MIN_DISTANCE_BARS = {new_params['MIN_DISTANCE_BARS']}", flush=True)
        print(f"  ENTRY_TOLERANCE_USD = {new_params['ENTRY_TOLERANCE_USD']}", flush=True)
        print(f"  num_signals = {num_signals} (long={long_c}, short={short_c})",
              flush=True)
        print(f"  avg_per_day = {avg:.2f}", flush=True)
        print(f"  ({elapsed:.1f} сек)", flush=True)

        period_results.append({
            "period_name": period_name,
            "params": new_params,
        })

        block = format_period_block(period_name, days, old_params,
                                    new_params, num_signals)
        report_lines.append(block)

    report_lines.append("")
    report_lines.append(format_summary_table(period_results))

    report_text = "\n".join(report_lines)

    print("\n" + report_text, flush=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_name = f"{ts}_{tf}m_periods.txt"
    report_path = os.path.join(RESULTS_DIR, report_name)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\nОтчёт: {report_path}", flush=True)

    # Уборка временного файла
    if os.path.exists(PAIRS_YAML_LIVE):
        try:
            os.remove(PAIRS_YAML_LIVE)
        except Exception:
            pass

    return report_path


# ============================================================
#  main
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", type=str, default=None,
                        help="Таймфрейм: 15, 30 или 60. "
                             "По умолчанию из optimization/current_tf.txt")
    args = parser.parse_args()

    tf = args.tf if args.tf else read_current_tf()
    if tf not in {"15", "30", "60"}:
        print(f"ERROR: неподдерживаемый TF '{tf}'. Разрешено: 15, 30, 60",
              flush=True)
        sys.exit(1)

    try:
        run_all_periods(tf)
    except Exception as e:
        print(f"optimize_v2 упал: {type(e).__name__}: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()