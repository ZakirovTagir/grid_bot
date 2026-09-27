"""
optimize.py — оптимизатор параметров стратегии свинг-точек.

- lock-файл против параллельных запусков;
- прогресс в Telegram раз в 6 часов;
- перебор полного grid на IS;
- OOS-проверка топ-30;
- при успехе — pairs_candidate_{tf}.yaml + уведомление.

Запуск (на сервере):
    source venv/bin/activate
    nohup python -u optimize.py --tf 15 --workers 2 > optimization/run_15m.log 2>&1 &
"""

import os
import sys
import io
import time
import argparse
import itertools
import signal
import contextlib
import traceback
import multiprocessing as mp
import urllib.request
import json
from datetime import datetime
from typing import Dict, List, Any, Optional

import pandas as pd
import yaml
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from find_swing_points import run_backtest, DEFAULT_PARAMS
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
IS_MONTHS = 4
OOS_MONTHS = 2
TOTAL_DAYS = (IS_MONTHS + OOS_MONTHS) * 30

TOP_N_FOR_OOS = 30
OOS_MIN_TRADES = 10
OOS_MIN_NET_PNL = 0.0

PROGRESS_INTERVAL_SEC = 6 * 3600

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
#  Telegram
# ============================================================
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[tg] skipped (no token/chat_id): {text[:80]}", flush=True)
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
#  Сетки
# ============================================================
GRIDS: Dict[str, Dict[str, List[Any]]] = {
    "60": {
        "N": [5, 7, 9],
        "MIN_BODY_RATIO": [0.1, 0.3, 0.5],
        "DELTA_PRICE": [50, 100, 200, 500, 1000],
        "MIN_DISTANCE_BARS": [3, 5, 10],
        "MAX_DELTA_EXTREMES": [300, 500, 1000, 2000, 5000],
        "ENTRY_TOLERANCE_USD": [50, 200, 500, 1000],
        "MIN_BARS_AFTER_POINT2": [2, 3, 5],
        "MAX_BARS_AFTER_POINT2": [5, 7, 10],
    },
    "30": {
        "N": [5, 7, 9],
        "MIN_BODY_RATIO": [0.1, 0.3, 0.5],
        "DELTA_PRICE": [25, 50, 100, 250, 500],
        "MIN_DISTANCE_BARS": [3, 5, 10],
        "MAX_DELTA_EXTREMES": [150, 250, 500, 1000, 2500],
        "ENTRY_TOLERANCE_USD": [25, 100, 250, 500],
        "MIN_BARS_AFTER_POINT2": [2, 3, 5],
        "MAX_BARS_AFTER_POINT2": [5, 7, 10],
    },
    "15": {
        "N": [5, 7, 9],
        "MIN_BODY_RATIO": [0.1, 0.3, 0.5],
        "DELTA_PRICE": [12, 25, 50, 120, 250],
        "MIN_DISTANCE_BARS": [3, 5, 10],
        "MAX_DELTA_EXTREMES": [75, 125, 250, 500, 1250],
        "ENTRY_TOLERANCE_USD": [12, 50, 125, 250],
        "MIN_BARS_AFTER_POINT2": [2, 3, 5],
        "MAX_BARS_AFTER_POINT2": [5, 7, 10],
    },
}


# ============================================================
#  Вспомогательное
# ============================================================
def read_current_tf() -> str:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            tf = f.read().strip()
        if tf in GRIDS:
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


def load_and_split(csv_path: str, is_months: int, oos_months: int):
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    end_date = df["timestamp"].max()
    total_months = is_months + oos_months
    is_start = end_date - pd.DateOffset(months=total_months)
    oos_start = end_date - pd.DateOffset(months=oos_months)

    is_df = df[(df["timestamp"] >= is_start) & (df["timestamp"] < oos_start)].copy()
    oos_df = df[df["timestamp"] >= oos_start].copy()
    is_df.reset_index(drop=True, inplace=True)
    oos_df.reset_index(drop=True, inplace=True)
    return is_df, oos_df


def compute_score(metrics: Dict[str, Any]) -> float:
    net_pnl = metrics["net_pnl"]
    if net_pnl <= 0:
        return float(net_pnl)
    num_trades = metrics["num_trades"]
    win_rate = metrics["win_rate"]
    max_dd = metrics["max_drawdown_pct"]
    trade_factor = min(1.0, num_trades / 30.0)
    dd_factor = max(0.0, 1.0 - max_dd / 100.0)
    wr_factor = win_rate ** 0.5
    return net_pnl * trade_factor * dd_factor * wr_factor


def generate_combinations(grid: Dict[str, List[Any]]):
    keys = list(grid.keys())
    for combo in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, combo))


# ============================================================
#  Multiprocessing worker
# ============================================================
_IS_DF = None
_BALANCE = 1000.0


def _worker_init(is_df, balance):
    global _IS_DF, _BALANCE
    _IS_DF = is_df
    _BALANCE = balance


def _worker(combo):
    params = dict(DEFAULT_PARAMS)
    params.update(combo)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            metrics = run_backtest(params, _IS_DF, initial_balance=_BALANCE,
                                   save_events_path=None, verbose=False)
    except Exception as e:
        return {
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc()[:800],
            "combo": combo,
        }
    row = dict(combo)
    row.update({
        "score": compute_score(metrics),
        "net_pnl": metrics["net_pnl"],
        "num_trades": metrics["num_trades"],
        "win_rate": metrics["win_rate"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
    })
    return row


# ============================================================
#  Кандидат
# ============================================================
PARAM_KEYS_ORDER = [
    "N", "MIN_BODY_RATIO", "DELTA_PRICE", "MIN_DISTANCE_BARS", "MAX_DELTA_EXTREMES",
    "ENTRY_TOLERANCE_USD", "MIN_BARS_AFTER_POINT2", "MAX_BARS_AFTER_POINT2",
]


def write_candidate(tf: str, best_params: Dict[str, Any],
                    is_metrics: Dict, oos_metrics: Dict) -> str:
    with open(PAIRS_YAML, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "BTCUSDT" not in cfg:
        cfg["BTCUSDT"] = {}
    for k, v in best_params.items():
        cfg["BTCUSDT"][k] = v
    cfg["BTCUSDT"]["_optimization"] = {
        "tf": tf,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_score": round(compute_score(is_metrics), 3),
        "is_net_pnl": is_metrics["net_pnl"],
        "is_trades": is_metrics["num_trades"],
        "is_win_rate": is_metrics["win_rate"],
        "oos_net_pnl": oos_metrics["net_pnl"],
        "oos_trades": oos_metrics["num_trades"],
        "oos_win_rate": oos_metrics["win_rate"],
    }
    out_path = os.path.join(CONFIG_DIR, f"pairs_candidate_{tf}m.yaml")
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
    return out_path


# ============================================================
#  Основной цикл
# ============================================================
def run_optimization(tf: str, workers: int = 1, limit: Optional[int] = None,
                     initial_balance: float = 1000.0):
    csv_path = ensure_csv(tf, TOTAL_DAYS)
    is_df, oos_df = load_and_split(csv_path, IS_MONTHS, OOS_MONTHS)
    print(f"IS : {len(is_df)} свечей ({is_df['timestamp'].min()} .. {is_df['timestamp'].max()})", flush=True)
    print(f"OOS: {len(oos_df)} свечей ({oos_df['timestamp'].min()} .. {oos_df['timestamp'].max()})", flush=True)

    combos = list(generate_combinations(GRIDS[tf]))
    if limit is not None:
        combos = combos[:limit]
    total = len(combos)
    print(f"Всего комбинаций: {total}, workers={workers}", flush=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    is_csv = os.path.join(RESULTS_DIR, f"{ts}_{tf}m_IS.csv")
    oos_csv = os.path.join(RESULTS_DIR, f"{ts}_{tf}m_OOS.csv")

    tg_send(f"Оптимизатор запущен: TF={tf}m, комбо={total}, workers={workers}")

    results: List[Dict[str, Any]] = []
    errors_printed = 0
    t_start = time.time()
    last_report = t_start

    def _handle_row(row, i):
        nonlocal last_report, errors_printed
        if row is None:
            return
        if isinstance(row, dict) and "error" in row:
            if errors_printed < 3:
                print(f"[error #{i}] {row['error']}", flush=True)
                print(row.get("trace", "")[:600], flush=True)
                print(f"combo: {row['combo']}", flush=True)
                errors_printed += 1
            return
        results.append(row)

        if (i + 1) % 50 == 0 or (i + 1) == total:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            best = max((r['score'] for r in results), default=float('-inf'))
            print(f"[{i+1}/{total}] ok={len(results)} elapsed={elapsed:.0f}s "
                  f"ETA={eta:.0f}s best={best:.2f}", flush=True)

        if time.time() - last_report > PROGRESS_INTERVAL_SEC:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            best = max((r['score'] for r in results), default=0)
            tg_send(f"Оптимизатор [{tf}m]: {i+1}/{total} "
                    f"({(i+1)/total*100:.1f}%), ETA {eta/3600:.1f}ч, "
                    f"best_score={best:.2f}, ok={len(results)}")
            last_report = time.time()

    if workers > 1:
        with mp.Pool(workers, initializer=_worker_init,
                     initargs=(is_df, initial_balance)) as pool:
            for i, row in enumerate(pool.imap_unordered(_worker, combos, chunksize=5)):
                _handle_row(row, i)
    else:
        _worker_init(is_df, initial_balance)
        for i, combo in enumerate(combos):
            row = _worker(combo)
            _handle_row(row, i)

    is_df_result = pd.DataFrame(results)
    if not is_df_result.empty:
        is_df_result.sort_values("score", ascending=False, inplace=True)
    is_df_result.to_csv(is_csv, index=False)
    print(f"\nIS результатов: {len(is_df_result)} (из {total} комбо, "
          f"ошибок: {errors_printed if errors_printed < 3 else '3+'} )", flush=True)
    print(f"IS → {is_csv}", flush=True)
    print(f"Total time (IS): {time.time() - t_start:.1f}s", flush=True)

    if is_df_result.empty:
        tg_send(f"Оптимизатор [{tf}m]: пустой результат, кандидат не создан")
        return

    print("\n=== TOP-5 (IS) ===", flush=True)
    for _, r in is_df_result.head(5).iterrows():
        print(f"score={r['score']:.2f} net={r['net_pnl']:.2f} "
              f"trades={r['num_trades']:.0f} wr={r['win_rate']:.3f} "
              f"dd={r['max_drawdown_pct']:.2f}%", flush=True)

    # ---- OOS-проверка топ-N ----
    top = is_df_result.head(TOP_N_FOR_OOS)
    print(f"\nOOS-проверка топ-{len(top)}...", flush=True)

    oos_results = []
    for _, row in top.iterrows():
        params = dict(DEFAULT_PARAMS)
        for k in PARAM_KEYS_ORDER:
            params[k] = row[k]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                m = run_backtest(params, oos_df, initial_balance=initial_balance,
                                 save_events_path=None, verbose=False)
        except Exception:
            continue
        rec = {k: row[k] for k in PARAM_KEYS_ORDER}
        rec.update({
            "is_score": row["score"],
            "is_net_pnl": row["net_pnl"],
            "oos_net_pnl": m["net_pnl"],
            "oos_trades": m["num_trades"],
            "oos_win_rate": m["win_rate"],
            "oos_max_dd": m["max_drawdown_pct"],
            "passed": (m["net_pnl"] > OOS_MIN_NET_PNL and m["num_trades"] >= OOS_MIN_TRADES),
        })
        oos_results.append(rec)

    oos_df_result = pd.DataFrame(oos_results)
    oos_df_result.to_csv(oos_csv, index=False)
    print(f"OOS результатов: {len(oos_df_result)} → {oos_csv}", flush=True)

    passed = oos_df_result[oos_df_result["passed"] == True].sort_values(
        "oos_net_pnl", ascending=False)

    if passed.empty:
        tg_send(f"Оптимизатор [{tf}m] завершён. Улучшения не найдено: "
                f"0 из {len(oos_df_result)} кандидатов прошли OOS-фильтр.")
        print("\nУлучшения не найдено: ни один кандидат не прошёл OOS.", flush=True)
        return

    winner = passed.iloc[0]
    best_params = {k: winner[k] for k in PARAM_KEYS_ORDER}

    params = dict(DEFAULT_PARAMS)
    params.update(best_params)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        is_m = run_backtest(params, is_df, initial_balance=initial_balance, verbose=False)
        oos_m = run_backtest(params, oos_df, initial_balance=initial_balance, verbose=False)

    cand_path = write_candidate(tf, best_params, is_m, oos_m)
    print(f"\nКандидат записан: {cand_path}", flush=True)

    with open(PAIRS_YAML, "r", encoding="utf-8") as f:
        current_cfg = yaml.safe_load(f) or {}
    current_btc = current_cfg.get("BTCUSDT", {})

    lines = [f"НОВЫЙ КАНДИДАТ [{tf}m]",
             f"TF: {tf}m",
             f"IS: net={is_m['net_pnl']:.2f} trades={is_m['num_trades']} "
             f"wr={is_m['win_rate']:.3f}",
             f"OOS: net={oos_m['net_pnl']:.2f} trades={oos_m['num_trades']} "
             f"wr={oos_m['win_rate']:.3f}",
             ""]
    for k in PARAM_KEYS_ORDER:
        old_v = current_btc.get(k, "?")
        new_v = best_params[k]
        lines.append(f"{k} = {old_v}/{new_v}")
    lines.append("")
    lines.append(f"Файл: {cand_path}")
    lines.append("Применить: /apply_candidate")

    tg_send("\n".join(lines))
    print("\n".join(lines), flush=True)


# ============================================================
#  main
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", type=str, default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--balance", type=float, default=1000.0)
    args = parser.parse_args()

    tf = args.tf if args.tf else read_current_tf()
    if tf not in GRIDS:
        print(f"ERROR: unsupported TF '{tf}'. Allowed: {list(GRIDS.keys())}", flush=True)
        sys.exit(1)

    if not acquire_lock():
        msg = f"Оптимизатор [{tf}m]: предыдущий запуск ещё работает, старт отменён."
        print(msg, flush=True)
        tg_send(msg)
        sys.exit(2)

    def _cleanup(signum, frame):
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        print(f"=== Оптимизатор === TF: {tf}m, workers={args.workers}, "
              f"IS: {IS_MONTHS} мес, OOS: {OOS_MONTHS} мес", flush=True)
        run_optimization(tf, workers=args.workers, limit=args.limit,
                         initial_balance=args.balance)
    finally:
        release_lock()


if __name__ == "__main__":
    mp.freeze_support()
    main()