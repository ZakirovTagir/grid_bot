"""
optimize.py — калькулятор параметров стратегии свинг-точек.

Логика:
- читает TF из optimization/current_tf.txt (или --tf);
- при расчёте каждого параметра ВСЕ остальные отключены;
- scan mode: оба искателя работают параллельно;
- считает 4 параметра:
    DELTA_PRICE        — 30 дней — медиана амплитуд блоков (trimmed 10%) × 0.25
    MIN_DISTANCE_BARS  — 90 дней — 25-й процентиль расстояний (trimmed 10%)
    ENTRY_TOLERANCE_USD— 30 дней — медиана недоходов до линии (trimmed 10%) × 1.5
    MAX_DELTA_EXTREMES — жёстко 4.0
- в конце — финальный бэктест с рассчитанными параметрами;
- пишет config/pairs_candidate_{tf}m.yaml;
- пишет txt-отчёт в optimization/results/;
- загружает кандидата и отчёт на Яндекс.Диск;
- отправляет TG-уведомления (старт и финал);
- lock-файл для защиты от параллельных запусков.
"""

import os
import sys
import io
import time
import argparse
import signal
import contextlib
import traceback
import urllib.request
import json
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from find_swing_points import (
    run_backtest, DEFAULT_PARAMS,
    collect_block_amplitudes,
    collect_point_distances,
    collect_body_ratios,
    collect_line_gaps,
)
from scripts.fetch_history import fetch_history

load_dotenv(os.path.join(BASE_DIR, ".env"))


# ============================================================
#  Константы
# ============================================================
OPTIMIZATION_DIR = os.path.join(BASE_DIR, "optimization")
RESULTS_DIR = os.path.join(OPTIMIZATION_DIR, "results")
STATE_FILE = os.path.join(OPTIMIZATION_DIR, "current_tf.txt")
LOCK_FILE = os.path.join(OPTIMIZATION_DIR, "optimize.lock")
HISTORICAL_DIR = os.path.join(BASE_DIR, "data", "historical")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
PAIRS_YAML = os.path.join(CONFIG_DIR, "pairs.yaml")

os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(OPTIMIZATION_DIR, exist_ok=True)

DEFAULT_TF = "60"
ALLOWED_TF = {"60", "30", "15"}

# Периоды расчёта
DAYS_DELTA = 30
DAYS_DISTANCE = 90
DAYS_TOLERANCE = 30

# Коэффициенты
DELTA_K = 0.25
TOLERANCE_K = 1.5
TRIM_PCT = 0.10

# Жёстко захардкоженные
HARD_N = 7
HARD_MIN_BODY_RATIO = 0.3
HARD_MAX_DELTA_EXTREMES = 4.0
HARD_MIN_BARS_AFTER = 3
HARD_MAX_BARS_AFTER = 10

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
YADISK_TOKEN = os.getenv("YADISK_TOKEN")


# ============================================================
#  Telegram
# ============================================================
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[tg] skipped: {text[:80]}", flush=True)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"[tg] send failed: {e}", flush=True)


# ============================================================
#  Lock
# ============================================================
def acquire_lock() -> bool:
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# ============================================================
#  Вспомогательное
# ============================================================
def read_current_tf() -> str:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            tf = f.read().strip()
        if tf in ALLOWED_TF:
            return tf
    return DEFAULT_TF


def get_csv_path(tf: str) -> str:
    return os.path.join(HISTORICAL_DIR, f"BTCUSDT_{tf}.csv")


def ensure_csv(tf: str, days_back: int) -> str:
    path = get_csv_path(tf)
    if os.path.exists(path):
        age_hours = (time.time() - os.path.getmtime(path)) / 3600
        if age_hours < 24:
            print(f"CSV актуален ({age_hours:.1f} ч): {path}", flush=True)
            return path
    print(f"Скачиваю свечи: TF={tf}, {days_back} дней", flush=True)
    fetch_history("BTCUSDT", tf, days_back, path, verbose=True)
    return path


def load_df(csv_path: str, days_back: int) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    # берём последние days_back дней
    end = df["timestamp"].max()
    start = end - pd.Timedelta(days=days_back)
    df = df[df["timestamp"] >= start].reset_index(drop=True)
    return df


def trimmed_percentile(values: List[float], pct: float,
                       trim_pct: float = TRIM_PCT) -> float:
    """Trimmed percentile: отбрасываем по trim_pct с каждой стороны, берём pct."""
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
#  Расчёт параметров
# ============================================================
def calc_delta_price(tf: str) -> float:
    """Медиана амплитуд блоков (30 дней) × 0.25."""
    print(f"\n[CALC] DELTA_PRICE — окно {DAYS_DELTA} дней", flush=True)
    csv_path = ensure_csv(tf, DAYS_DISTANCE)   # берём больше данных, обрежем
    df = load_df(csv_path, DAYS_DELTA)

    amplitudes = collect_block_amplitudes(df, n=HARD_N)
    med = trimmed_median(amplitudes)
    delta = med * DELTA_K

    print(f"  собрано амплитуд блоков: {len(amplitudes)}", flush=True)
    print(f"  медиана (trimmed {int(TRIM_PCT*100)}%): {med:.2f}", flush=True)
    print(f"  DELTA_PRICE = {med:.2f} × {DELTA_K} = {delta:.2f}", flush=True)
    return round(delta, 2)


def calc_min_distance_bars(tf: str) -> int:
    """25-й процентиль расстояний между точками (90 дней)."""
    print(f"\n[CALC] MIN_DISTANCE_BARS — окно {DAYS_DISTANCE} дней", flush=True)
    csv_path = ensure_csv(tf, DAYS_DISTANCE)
    df = load_df(csv_path, DAYS_DISTANCE)

    base = dict(DEFAULT_PARAMS)
    base['N'] = HARD_N
    base['MIN_BODY_RATIO'] = HARD_MIN_BODY_RATIO
    distances = collect_point_distances(df, base)
    p25 = trimmed_percentile(distances, 0.25)

    print(f"  собрано расстояний: {len(distances)}", flush=True)
    if distances:
        print(f"  min={min(distances)}, max={max(distances)}", flush=True)
    print(f"  MIN_DISTANCE_BARS = 25-й перцентиль = {p25:.2f}", flush=True)
    return int(round(p25))


def calc_entry_tolerance(tf: str) -> float:
    """Медиана недоходов до линии (30 дней) × 1.5."""
    print(f"\n[CALC] ENTRY_TOLERANCE_USD — окно {DAYS_TOLERANCE} дней", flush=True)
    csv_path = ensure_csv(tf, DAYS_DISTANCE)
    df = load_df(csv_path, DAYS_TOLERANCE)

    base = dict(DEFAULT_PARAMS)
    base['N'] = HARD_N
    base['MIN_BODY_RATIO'] = HARD_MIN_BODY_RATIO
    base['MIN_BARS_AFTER_POINT2'] = HARD_MIN_BARS_AFTER
    base['MAX_BARS_AFTER_POINT2'] = HARD_MAX_BARS_AFTER

    gaps = collect_line_gaps(df, base)
    med = trimmed_median(gaps)
    tol = med * TOLERANCE_K

    print(f"  собрано недоходов: {len(gaps)}", flush=True)
    print(f"  медиана (trimmed): {med:.2f}", flush=True)
    print(f"  ENTRY_TOLERANCE_USD = {med:.2f} × {TOLERANCE_K} = {tol:.2f}", flush=True)
    return round(tol, 2)


# ============================================================
#  Кандидат
# ============================================================
PARAM_KEYS_ORDER = [
    "N", "MIN_BODY_RATIO", "DELTA_PRICE", "MIN_DISTANCE_BARS", "MAX_DELTA_EXTREMES",
    "ENTRY_TOLERANCE_USD", "MIN_BARS_AFTER_POINT2", "MAX_BARS_AFTER_POINT2",
]


def write_candidate(tf: str, new_params: Dict[str, Any], metrics: Dict) -> str:
    with open(PAIRS_YAML, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "BTCUSDT" not in cfg:
        cfg["BTCUSDT"] = {}
    for k, v in new_params.items():
        cfg["BTCUSDT"][k] = v
    cfg["BTCUSDT"]["_optimization"] = {
        "tf": tf,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "num_trades": metrics["num_trades"],
        "net_pnl": metrics["net_pnl"],
        "win_rate": metrics["win_rate"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
    }
    out_path = os.path.join(CONFIG_DIR, f"pairs_candidate_{tf}m.yaml")
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
    return out_path


# ============================================================
#  Отчёт
# ============================================================
def build_report(tf: str, old_params: Dict, new_params: Dict,
                 metrics: Dict, durations: Dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"ОТЧЁТ РАСЧЁТА ПАРАМЕТРОВ  [{tf}m]")
    lines.append(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    lines.append("")
    lines.append("--- СТАРЫЕ ЗНАЧЕНИЯ ---")
    for k in PARAM_KEYS_ORDER:
        lines.append(f"  {k} = {old_params.get(k, '?')}")
    lines.append("")
    lines.append("--- НОВЫЕ ЗНАЧЕНИЯ ---")
    for k in PARAM_KEYS_ORDER:
        old_v = old_params.get(k, '?')
        new_v = new_params.get(k, '?')
        lines.append(f"  {k} = {old_v}/{new_v}")
    lines.append("")
    lines.append("--- ВРЕМЯ РАСЧЁТА ---")
    for k, t in durations.items():
        lines.append(f"  {k}: {t:.1f} сек")
    lines.append("")
    lines.append("--- ФИНАЛЬНЫЙ БЭКТЕСТ (с рассчитанными параметрами) ---")
    lines.append(f"  net_pnl: {metrics['net_pnl']} USDT")
    lines.append(f"  final_balance: {metrics['final_balance']} USDT")
    lines.append(f"  num_trades: {metrics['num_trades']}")
    lines.append(f"  win_rate: {metrics['win_rate']}")
    lines.append(f"  max_drawdown_pct: {metrics['max_drawdown_pct']}%")
    lines.append("")
    lines.append("Применить: /apply_candidate")
    lines.append("=" * 60)
    return "\n".join(lines)


# ============================================================
#  Яндекс.Диск
# ============================================================
def upload_to_yadisk(local_path: str, remote_path: str) -> bool:
    if not YADISK_TOKEN:
        print(f"[yadisk] skipped (no token): {remote_path}", flush=True)
        return False
    try:
        from utils.yadisk_sync import YaDiskSync
        yd = YaDiskSync(YADISK_TOKEN, local_path, remote_path=remote_path)
        if yd.client.exists(remote_path):
            yd.client.remove(remote_path)
        return yd.upload_file(local_path, remote_path)
    except Exception as e:
        print(f"[yadisk] upload failed: {e}", flush=True)
        return False


# ============================================================
#  Основной цикл
# ============================================================
def run_calc(tf: str):
    print(f"=== Калькулятор параметров === TF: {tf}m", flush=True)
    print(f"Окна: DELTA={DAYS_DELTA}д, DISTANCE={DAYS_DISTANCE}д, TOLERANCE={DAYS_TOLERANCE}д",
          flush=True)

    tg_send(f"Калькулятор параметров запущен: TF={tf}m")

    # Старые параметры
    with open(PAIRS_YAML, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    old_params = dict(cfg.get("BTCUSDT", {}))

    t_start = time.time()
    durations = {}

    # 1. DELTA_PRICE
    t0 = time.time()
    new_delta = calc_delta_price(tf)
    durations['DELTA_PRICE'] = time.time() - t0

    # 2. MIN_DISTANCE_BARS
    t0 = time.time()
    new_distance = calc_min_distance_bars(tf)
    durations['MIN_DISTANCE_BARS'] = time.time() - t0

    # 3. ENTRY_TOLERANCE_USD
    t0 = time.time()
    new_tolerance = calc_entry_tolerance(tf)
    durations['ENTRY_TOLERANCE_USD'] = time.time() - t0

    # Сборка новых параметров
    new_params = {
        'N': HARD_N,
        'MIN_BODY_RATIO': HARD_MIN_BODY_RATIO,
        'DELTA_PRICE': new_delta,
        'MIN_DISTANCE_BARS': new_distance,
        'MAX_DELTA_EXTREMES': HARD_MAX_DELTA_EXTREMES,
        'ENTRY_TOLERANCE_USD': new_tolerance,
        'MIN_BARS_AFTER_POINT2': HARD_MIN_BARS_AFTER,
        'MAX_BARS_AFTER_POINT2': HARD_MAX_BARS_AFTER,
    }

    print(f"\n=== РАСЧЁТ ЗАВЕРШЁН за {time.time()-t_start:.1f} сек ===", flush=True)
    for k in PARAM_KEYS_ORDER:
        print(f"  {k} = {old_params.get(k, '?')} → {new_params[k]}", flush=True)

    # 4. Финальный бэктест с рассчитанными параметрами
    print(f"\n[BACKTEST] прогон с рассчитанными параметрами...", flush=True)
    csv_path = ensure_csv(tf, DAYS_DISTANCE)
    df = load_df(csv_path, DAYS_DISTANCE)

    params = dict(DEFAULT_PARAMS)
    params.update(new_params)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        metrics = run_backtest(params, df, initial_balance=1000.0, verbose=False)

    print(f"  net_pnl: {metrics['net_pnl']} USDT", flush=True)
    print(f"  num_trades: {metrics['num_trades']}", flush=True)
    print(f"  win_rate: {metrics['win_rate']}", flush=True)
    print(f"  max_drawdown_pct: {metrics['max_drawdown_pct']}%", flush=True)

    # 5. Кандидат
    cand_path = write_candidate(tf, new_params, metrics)
    print(f"\nКандидат: {cand_path}", flush=True)

    # 6. Отчёт в txt
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_name = f"{ts}_{tf}m_calc_report.txt"
    report_path = os.path.join(RESULTS_DIR, report_name)
    report_text = build_report(tf, old_params, new_params, metrics, durations)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"Отчёт: {report_path}", flush=True)

    # 7. Загрузка на Яндекс.Диск
    upload_to_yadisk(report_path, f"grid_bot/reports/{report_name}")
    upload_to_yadisk(cand_path, f"grid_bot/config/pairs_candidate_{tf}m.yaml")

    # 8. TG-отчёт
    lines = [f"РАСЧЁТ ЗАВЕРШЁН [{tf}m]",
             f"net={metrics['net_pnl']:.2f} trades={metrics['num_trades']} "
             f"wr={metrics['win_rate']:.3f} dd={metrics['max_drawdown_pct']:.2f}%",
             ""]
    for k in PARAM_KEYS_ORDER:
        lines.append(f"{k} = {old_params.get(k, '?')}/{new_params[k]}")
    lines.append("")
    lines.append(f"Файл: pairs_candidate_{tf}m.yaml")
    lines.append("Применить: /apply_candidate")
    tg_send("\n".join(lines))

    print(f"\nTotal: {time.time()-t_start:.1f} сек", flush=True)


# ============================================================
#  main
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", type=str, default=None)
    args = parser.parse_args()

    tf = args.tf if args.tf else read_current_tf()
    if tf not in ALLOWED_TF:
        print(f"ERROR: unsupported TF '{tf}'. Allowed: {sorted(ALLOWED_TF)}", flush=True)
        sys.exit(1)

    if not acquire_lock():
        msg = f"Калькулятор [{tf}m]: предыдущий запуск ещё работает, старт отменён."
        print(msg, flush=True)
        tg_send(msg)
        sys.exit(2)

    def _cleanup(signum, frame):
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        run_calc(tf)
    except Exception as e:
        err = f"Калькулятор [{tf}m] упал: {type(e).__name__}: {e}"
        print(err, flush=True)
        print(traceback.format_exc()[:1000], flush=True)
        tg_send(err)
    finally:
        release_lock()


if __name__ == "__main__":
    main()