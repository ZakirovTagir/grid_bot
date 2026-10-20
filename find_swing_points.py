"""
find_swing_points.py
Бэктест стратегии на основе свинг-точек.

v9:
- [FIX #6] Добавлен MAX_STRUCTURE_MOVE_PCT. Если импульс > 1.5%,
  max1 сбрасывается.
- Синхронизация логики с core/swing_finder.py v4.
"""
import math
import pandas as pd
import sys
from typing import Dict, Any, List, Optional

DEFAULT_PARAMS = {
    'N': 7,
    'MIN_BODY_RATIO': 0.3,
    'DELTA_PRICE': 117.53,
    'MIN_DISTANCE_BARS': 10,
    'MAX_DELTA_EXTREMES': 4.0,
    'ENTRY_TOLERANCE_USD': 68.7,
    'MIN_BARS_AFTER_POINT2': 3,
    'MAX_BARS_AFTER_POINT2': 10,
    'MAX_STRUCTURE_MOVE_PCT': 1.5,       # [FIX #6]
    'MAX_LOSS_PER_TRADE': 8.0,
    'MAX_STOP_DISTANCE_PERCENT': 2.5,
    'MAX_POSITION_PERCENT': 95.0,
    'MIN_POSITION_USDT': 10.0,
    'MIN_TARGET_PROFIT': 200.0,
    'TARGET_BARS_DELAY': 3,
    'MANUAL_TARGET_PRICE': 0.0,
}

TAKER_FEE = 0.00055
SLIPPAGE_BUFFER_USDT = 10.0
QTY_STEP = 0.001
INPUT_CSV = "data/historical/BTCUSDT_15.csv"
OUTPUT_LOG = "swing_points_log.txt"

def is_noisy(row, min_body_ratio: float) -> bool:
    high = row['high']
    low = row['low']
    open_ = row['open']
    close = row['close']
    candle_range = high - low
    if candle_range <= 0:
        return True
    body = abs(close - open_)
    return (body / candle_range) < min_body_ratio

def get_block_non_noisy(block, min_body_ratio: float):
    non_noisy = []
    for idx, row in block.iterrows():
        if not is_noisy(row, min_body_ratio):
            non_noisy.append((idx, row))
    return non_noisy

def get_block_extremes(non_noisy):
    if not non_noisy:
        return None, None
    min_row = min(non_noisy, key=lambda x: (x[1]['low'], x[0]))
    max_row = max(non_noisy, key=lambda x: (x[1]['high'], -x[0]))
    return (min_row[1]['low'], min_row[0]), (max_row[1]['high'], max_row[0])

def candle_body(df: pd.DataFrame, idx: int) -> float:
    row = df.iloc[idx]
    return abs(float(row['close']) - float(row['open']))

def avg_body_between(df: pd.DataFrame, idx_start: int, idx_end: int) -> float:
    if idx_end < idx_start:
        return 0.0
    bodies = [candle_body(df, i) for i in range(idx_start, idx_end + 1)]
    if not bodies:
        return 0.0
    return sum(bodies) / len(bodies)

def check_body_spike(df: pd.DataFrame, idx_point1: int, idx_point2: int, k: float) -> bool:
    if k <= 0:
        return False
    body_p2 = candle_body(df, idx_point2)
    avg_body = avg_body_between(df, idx_point1, idx_point2)
    if avg_body <= 0:
        return False
    return (body_p2 / avg_body) > k

def calc_structural_target(side: str, min1_price: float, max1_price: float, min2_price: float, max2_price: float) -> float:
    impulse = max1_price - min1_price
    if impulse <= 0:
        return 0.0
    if side == 'LONG':
        return min2_price + impulse / 2.0
    else:
        return max2_price - impulse / 2.0

def floor_qty(qty: float, step: float = QTY_STEP) -> float:
    if step <= 0:
        return qty
    precision = 0
    s = f"{step}"
    if "." in s:
        precision = len(s.split(".")[1])
    qty_clean = round(qty, precision + 4)
    steps = round(qty_clean / step, 6)
    floored = math.floor(steps + 1e-9) * step
    return round(floored, precision)


class LongFinder:
    def __init__(self, df, params: Dict[str, Any]):
        self.df = df
        self.params = params
        self.reset()

    def reset(self):
        self.state = "WAIT_MIN1"
        self.min1_price = self.min1_idx = None
        self.max1_price = self.max1_idx = None
        self.min2_price = self.min2_idx = None
        self.pending_trend = None

    def process_block(self, block_start, block):
        if self.state == "WAIT_ENTRY":
            return

        min_body_ratio = self.params['MIN_BODY_RATIO']
        delta_price = self.params['DELTA_PRICE']
        min_distance = self.params['MIN_DISTANCE_BARS']
        max_delta_k = self.params['MAX_DELTA_EXTREMES']
        max_struct_pct = self.params.get('MAX_STRUCTURE_MOVE_PCT', 1.5) / 100.0

        non_noisy = get_block_non_noisy(block, min_body_ratio)
        if not non_noisy:
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)

        if self.state == "WAIT_MIN1":
            if H_idx > L_idx and (H_price - L_price) >= delta_price:
                self.min1_price, self.min1_idx = L_price, L_idx
                self.max1_price, self.max1_idx = H_price, H_idx
                self.state = "WAIT_MIN2"
            else:
                self.reset()
        elif self.state == "WAIT_MIN2":
            if self.max1_price is None:
                if H_price > self.min1_price:
                    self.max1_price, self.max1_idx = H_price, H_idx
            elif H_price > self.max1_price:
                impulse_pct = (H_price - self.min1_price) / self.min1_price
                if impulse_pct > max_struct_pct:
                    self.max1_price = None
                    self.max1_idx = None
                else:
                    self.max1_price, self.max1_idx = H_price, H_idx

            if L_price > self.min1_price:
                distance = L_idx - self.min1_idx
                if distance >= min_distance and L_idx > (self.max1_idx if self.max1_idx is not None else 0):
                    if check_body_spike(self.df, self.min1_idx, L_idx, max_delta_k):
                        self.reset()
                        return
                    self.min2_price, self.min2_idx = L_price, L_idx
                    self.state = "WAIT_ENTRY"
                    self.pending_trend = {
                        'type': 'LONG',
                        'min1': (self.min1_idx, self.min1_price),
                        'max1': (self.max1_idx, self.max1_price),
                        'min2': (self.min2_idx, self.min2_price),
                    }
            elif L_price < self.min1_price:
                self.reset()
            else:
                self.reset()

    def check_entry(self, current_idx, current_candle):
        if self.state != "WAIT_ENTRY":
            return None

        min_bars_after = self.params['MIN_BARS_AFTER_POINT2']
        max_bars_after = self.params['MAX_BARS_AFTER_POINT2']
        entry_tolerance = self.params['ENTRY_TOLERANCE_USD']

        if current_candle['low'] < self.min2_price:
            self.reset()
            return None
        bars_since = current_idx - self.min2_idx
        if bars_since < min_bars_after:
            return None
        if bars_since > max_bars_after:
            self.reset()
            return None

        t1, t2 = self.min1_idx, self.min2_idx
        support = self.min1_price + (self.min2_price - self.min1_price) * (current_idx - t1) / (t2 - t1)
        low = current_candle['low']
        high = current_candle['high']

        if low <= support + entry_tolerance and high >= support - entry_tolerance:
            channel_width = self.max1_price - self.min1_price
            return {
                'type': 'LONG',
                'entry_price': support,
                'entry_idx': current_idx,
                'min1': (self.min1_idx, self.min1_price),
                'max1': (self.max1_idx, self.max1_price),
                'min2': (self.min2_idx, self.min2_price),
                'channel_width': channel_width,
            }
        return None


class ShortFinder:
    def __init__(self, df, params: Dict[str, Any]):
        self.df = df
        self.params = params
        self.reset()

    def reset(self):
        self.state = "WAIT_MAX1"
        self.max1_price = self.max1_idx = None
        self.min1_price = self.min1_idx = None
        self.max2_price = self.max2_idx = None
        self.pending_trend = None

    def process_block(self, block_start, block):
        if self.state == "WAIT_ENTRY":
            return

        min_body_ratio = self.params['MIN_BODY_RATIO']
        delta_price = self.params['DELTA_PRICE']
        min_distance = self.params['MIN_DISTANCE_BARS']
        max_delta_k = self.params['MAX_DELTA_EXTREMES']
        max_struct_pct = self.params.get('MAX_STRUCTURE_MOVE_PCT', 1.5) / 100.0

        non_noisy = get_block_non_noisy(block, min_body_ratio)
        if not non_noisy:
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)

        if self.state == "WAIT_MAX1":
            if L_idx > H_idx and (H_price - L_price) >= delta_price:
                self.max1_price, self.max1_idx = H_price, H_idx
                self.min1_price, self.min1_idx = L_price, L_idx
                self.state = "WAIT_MAX2"
            else:
                self.reset()
        elif self.state == "WAIT_MAX2":
            if self.min1_price is None:
                if L_price < self.max1_price:
                    self.min1_price, self.min1_idx = L_price, L_idx
            elif L_price < self.min1_price:
                impulse_pct = (self.max1_price - L_price) / self.max1_price
                if impulse_pct > max_struct_pct:
                    self.min1_price = None
                    self.min1_idx = None
                else:
                    self.min1_price, self.min1_idx = L_price, L_idx

            if H_price < self.max1_price:
                distance = H_idx - self.max1_idx
                if distance >= min_distance and H_idx > (self.min1_idx if self.min1_idx is not None else 0):
                    if check_body_spike(self.df, self.max1_idx, H_idx, max_delta_k):
                        self.reset()
                        return
                    self.max2_price, self.max2_idx = H_price, H_idx
                    self.state = "WAIT_ENTRY"
                    self.pending_trend = {
                        'type': 'SHORT',
                        'max1': (self.max1_idx, self.max1_price),
                        'min1': (self.min1_idx, self.min1_price),
                        'max2': (self.max2_idx, self.max2_price),
                    }
            elif H_price > self.max1_price:
                self.reset()
            else:
                self.reset()

    def check_entry(self, current_idx, current_candle):
        if self.state != "WAIT_ENTRY":
            return None

        min_bars_after = self.params['MIN_BARS_AFTER_POINT2']
        max_bars_after = self.params['MAX_BARS_AFTER_POINT2']
        entry_tolerance = self.params['ENTRY_TOLERANCE_USD']

        if current_candle['high'] > self.max2_price:
            self.reset()
            return None
        bars_since = current_idx - self.max2_idx
        if bars_since < min_bars_after:
            return None
        if bars_since > max_bars_after:
            self.reset()
            return None

        t1, t2 = self.max1_idx, self.max2_idx
        resistance = self.max1_price + (self.max2_price - self.max1_price) * (current_idx - t1) / (t2 - t1)
        low = current_candle['low']
        high = current_candle['high']

        if low <= resistance + entry_tolerance and high >= resistance - entry_tolerance:
            channel_width = self.max1_price - self.min1_price
            return {
                'type': 'SHORT',
                'entry_price': resistance,
                'entry_idx': current_idx,
                'max1': (self.max1_idx, self.max1_price),
                'min1': (self.min1_idx, self.min1_price),
                'max2': (self.max2_idx, self.max2_price),
                'channel_width': channel_width,
            }
        return None

def scan_mode_pass(df: pd.DataFrame, params: Dict[str, Any], callback=None) -> None:
    N = params['N']
    long_finder = LongFinder(df, params)
    short_finder = ShortFinder(df, params)

    total = len(df)
    buffer = []
    block_start = 0

    for i in range(total):
        buffer.append(i)
        if len(buffer) == N:
            block = df.iloc[buffer]
            prev_long_state = long_finder.state
            prev_short_state = short_finder.state

            long_finder.process_block(block_start, block)
            short_finder.process_block(block_start, block)

            if prev_long_state != "WAIT_ENTRY" and long_finder.state == "WAIT_ENTRY":
                if callback is not None:
                    callback("LONG", long_finder)
                long_finder.reset()

            if prev_short_state != "WAIT_ENTRY" and short_finder.state == "WAIT_ENTRY":
                if callback is not None:
                    callback("SHORT", short_finder)
                short_finder.reset()

            buffer = []
            block_start = i + 1

def collect_block_amplitudes(df: pd.DataFrame, n: int = 7) -> List[float]:
    amplitudes = []
    total = len(df)
    for start in range(0, total - n + 1, n):
        block = df.iloc[start:start + n]
        amp = float(block['high'].max() - block['low'].min())
        amplitudes.append(amp)
    return amplitudes

def collect_point_distances(df: pd.DataFrame, base_params: Dict[str, Any]) -> List[int]:
    params = dict(base_params)
    params['DELTA_PRICE'] = 0
    params['MIN_DISTANCE_BARS'] = 0
    params['MAX_DELTA_EXTREMES'] = 0
    params['ENTRY_TOLERANCE_USD'] = 10**9
    params['MIN_BARS_AFTER_POINT2'] = 0
    params['MAX_BARS_AFTER_POINT2'] = 10**9

    distances = []
    def cb(direction, finder):
        if direction == "LONG":
            distances.append(int(finder.min2_idx - finder.min1_idx))
        else:
            distances.append(int(finder.max2_idx - finder.max1_idx))

    scan_mode_pass(df, params, callback=cb)
    return distances

def collect_body_ratios(df: pd.DataFrame, base_params: Dict[str, Any]) -> List[float]:
    params = dict(base_params)
    params['DELTA_PRICE'] = 0
    params['MIN_DISTANCE_BARS'] = 0
    params['MAX_DELTA_EXTREMES'] = 0
    params['ENTRY_TOLERANCE_USD'] = 10**9
    params['MIN_BARS_AFTER_POINT2'] = 0
    params['MAX_BARS_AFTER_POINT2'] = 10**9

    ratios = []
    def cb(direction, finder):
        if direction == "LONG":
            idx1, idx2 = finder.min1_idx, finder.min2_idx
        else:
            idx1, idx2 = finder.max1_idx, finder.max2_idx
        avg_body = avg_body_between(df, idx1, idx2)
        if avg_body <= 0:
            return
        body_p2 = candle_body(df, idx2)
        ratios.append(float(body_p2 / avg_body))

    scan_mode_pass(df, params, callback=cb)
    return ratios

def collect_line_gaps(df: pd.DataFrame, base_params: Dict[str, Any]) -> List[float]:
    params = dict(base_params)
    params['DELTA_PRICE'] = 0
    params['MIN_DISTANCE_BARS'] = 0
    params['MAX_DELTA_EXTREMES'] = 0

    gaps = []
    def cb(direction, finder):
        if direction == "LONG":
            idx_start = finder.min2_idx + 1
            idx_end = min(finder.min2_idx + params['MAX_BARS_AFTER_POINT2'], len(df) - 1)
            t1, t2 = finder.min1_idx, finder.min2_idx
            p1, p2 = finder.min1_price, finder.min2_price
            for i in range(idx_start, idx_end + 1):
                row = df.iloc[i]
                line = p1 + (p2 - p1) * (i - t1) / (t2 - t1)
                low, high = float(row['low']), float(row['high'])
                if low <= line <= high:
                    gaps.append(0.0)
                else:
                    gaps.append(float(min(abs(low - line), abs(high - line))))
        else:
            idx_start = finder.max2_idx + 1
            idx_end = min(finder.max2_idx + params['MAX_BARS_AFTER_POINT2'], len(df) - 1)
            t1, t2 = finder.max1_idx, finder.max2_idx
            p1, p2 = finder.max1_price, finder.max2_price
            for i in range(idx_start, idx_end + 1):
                row = df.iloc[i]
                line = p1 + (p2 - p1) * (i - t1) / (t2 - t1)
                low, high = float(row['low']), float(row['high'])
                if low <= line <= high:
                    gaps.append(0.0)
                else:
                    gaps.append(float(min(abs(low - line), abs(high - line))))

    scan_mode_pass(df, params, callback=cb)
    return gaps

def run_backtest(params: Dict[str, Any], df: pd.DataFrame, initial_balance: float = 1000.0, save_events_path: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
    N = params['N']
    MAX_STOP_DISTANCE_PERCENT = params['MAX_STOP_DISTANCE_PERCENT']
    MAX_POSITION_PERCENT = params['MAX_POSITION_PERCENT']
    MIN_POSITION_USDT = params['MIN_POSITION_USDT']
    MIN_TARGET_PROFIT = params['MIN_TARGET_PROFIT']
    TARGET_BARS_DELAY = params['TARGET_BARS_DELAY']
    MANUAL_TARGET_PRICE = params.get('MANUAL_TARGET_PRICE', 0.0)

    long_finder = LongFinder(df, params)
    short_finder = ShortFinder(df, params)

    events: List[Dict[str, Any]] = []
    position = None
    balance = initial_balance
    equity_curve: List[float] = [balance]

    total_candles = len(df)
    buffer: List[int] = []
    block_start = 0
    i = 0

    while i < total_candles:
        current_candle = df.iloc[i]

        if position is not None:
            position['bars_since_entry'] = position.get('bars_since_entry', 0) + 1
            side = position['side']
            entry = position['entry_price']
            qty = position['qty']
            stop_loss = position['stop_loss']
            target_price = position['target_price']
            balance_before_open = position['balance_before']
            high = current_candle['high']
            low = current_candle['low']

            stop_hit = False
            if side == 'LONG' and low <= stop_loss:
                stop_hit = True
            elif side == 'SHORT' and high >= stop_loss:
                stop_hit = True

            if stop_hit:
                exit_price = stop_loss
                commission = qty * exit_price * TAKER_FEE
                if side == 'LONG':
                    balance += qty * exit_price - commission
                else:
                    balance -= qty * exit_price + commission
                pnl_total = balance - balance_before_open
                events.append({
                    'type': 'exit', 'idx': i, 'price': exit_price, 'trend': side,
                    'details': f"reason: stop_loss, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
                })
                equity_curve.append(balance)
                position = None
                long_finder.reset()
                short_finder.reset()
                buffer.clear()
                block_start = i + 1
                i += 1
                continue

            if target_price > 0 and position['bars_since_entry'] >= TARGET_BARS_DELAY:
                target_hit = False
                if side == 'LONG' and high >= target_price:
                    target_hit = True
                elif side == 'SHORT' and low <= target_price:
                    target_hit = True

                if target_hit:
                    exit_price = target_price
                    commission = qty * exit_price * TAKER_FEE
                    if side == 'LONG':
                        balance += qty * exit_price - commission
                    else:
                        balance -= qty * exit_price + commission
                    pnl_total = balance - balance_before_open
                    events.append({
                        'type': 'exit', 'idx': i, 'price': exit_price, 'trend': side,
                        'details': f"reason: target, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
                    })
                    equity_curve.append(balance)
                    position = None
                    long_finder.reset()
                    short_finder.reset()
                    buffer.clear()
                    block_start = i + 1
                    i += 1
                    continue

        else:
            buffer.append(i)
            if len(buffer) == N:
                block = df.iloc[buffer]
                long_finder.process_block(block_start, block)
                short_finder.process_block(block_start, block)
                buffer.clear()
                block_start = i + 1

            for finder, _label in [(long_finder, 'LONG'), (short_finder, 'SHORT')]:
                signal = finder.check_entry(i, current_candle)
                if signal is not None:
                    signal_entry = signal['entry_price']
                    side = signal['type']

                    position_value = balance * MAX_POSITION_PERCENT / 100
                    max_loss_usdt = position_value * MAX_STOP_DISTANCE_PERCENT / 100
                    params['MAX_LOSS_PER_TRADE'] = round(max_loss_usdt, 2)
                    commissions = position_value * TAKER_FEE * 2
                    price_loss = max_loss_usdt - commissions - SLIPPAGE_BUFFER_USDT

                    if price_loss <= 0:
                        events.append({
                            'type': 'entry_cancelled', 'trend': side,
                            'details': (f"price_loss={price_loss:.2f} <= 0 "
                                        f"(pos={position_value:.2f} max_loss={max_loss_usdt:.2f} "
                                        f"comm={commissions:.2f})")
                        })
                        finder.reset()
                        continue

                    if position_value < MIN_POSITION_USDT:
                        events.append({
                            'type': 'entry_cancelled', 'trend': side,
                            'details': (f"малая позиция ({position_value:.2f} < {MIN_POSITION_USDT})")
                        })
                        finder.reset()
                        continue

                    qty_raw = position_value / signal_entry
                    qty = floor_qty(qty_raw, QTY_STEP)

                    if qty <= 0:
                        events.append({
                            'type': 'entry_cancelled', 'trend': side,
                            'details': f"qty={qty} после floor, пропуск"
                        })
                        finder.reset()
                        continue

                    stop_distance = price_loss / qty

                    if side == 'LONG':
                        stop_loss = signal_entry - stop_distance
                    else:
                        stop_loss = signal_entry + stop_distance

                    min1_p = signal['min1'][1]
                    max1_p = signal['max1'][1]
                    if side == 'LONG':
                        min2_p = signal['min2'][1]
                        max2_p = 0.0
                    else:
                        min2_p = 0.0
                        max2_p = signal['max2'][1]

                    if MANUAL_TARGET_PRICE > 0:
                        candidate = MANUAL_TARGET_PRICE
                    else:
                        candidate = calc_structural_target(side, min1_p, max1_p, min2_p, max2_p)

                    if side == 'LONG':
                        floor_price = signal_entry + MIN_TARGET_PROFIT
                        target_price = (max(candidate, floor_price) if candidate > 0 else floor_price)
                    else:
                        ceiling_price = signal_entry - MIN_TARGET_PROFIT
                        target_price = (min(candidate, ceiling_price) if candidate > 0 else ceiling_price)

                    commission_open = position_value * TAKER_FEE
                    balance_before = balance
                    if side == 'LONG':
                        balance -= position_value + commission_open
                    else:
                        balance += position_value - commission_open

                    position = {
                        'side': side, 'entry_price': signal_entry, 'qty': qty,
                        'stop_loss': stop_loss, 'target_price': target_price,
                        'balance_before': balance_before, 'entry_idx': i, 'bars_since_entry': 0,
                    }

                    trend_points = finder.pending_trend
                    if trend_points:
                        if trend_points['type'] == 'LONG':
                            events.append({'type': 'мин1', 'idx': trend_points['min1'][0], 'price': trend_points['min1'][1], 'trend': 'LONG'})
                            events.append({'type': 'макс1', 'idx': trend_points['max1'][0], 'price': trend_points['max1'][1], 'trend': 'LONG'})
                            events.append({'type': 'мин2', 'idx': trend_points['min2'][0], 'price': trend_points['min2'][1], 'trend': 'LONG'})
                        else:
                            events.append({'type': 'макс1', 'idx': trend_points['max1'][0], 'price': trend_points['max1'][1], 'trend': 'SHORT'})
                            events.append({'type': 'мин1', 'idx': trend_points['min1'][0], 'price': trend_points['min1'][1], 'trend': 'SHORT'})
                            events.append({'type': 'макс2', 'idx': trend_points['max2'][0], 'price': trend_points['max2'][1], 'trend': 'SHORT'})

                    events.append({
                        'type': 'entry', 'idx': i, 'price': signal_entry, 'trend': side,
                        'details': (f"stop_loss:{stop_loss:.2f} (dist:{stop_distance:.2f}), "
                                    f"target:{target_price:.2f}, "
                                    f"qty:{qty:.6f}, pos_value:{position_value:.2f}, "
                                    f"max_loss:{max_loss_usdt:.2f}, price_loss:{price_loss:.2f}, "
                                    f"comm:{commissions:.2f}, "
                                    f"channel_width:{signal.get('channel_width', 0):.2f}")
                    })

                    finder.reset()
                    break

        i += 1

    if position is not None:
        last_price = df.iloc[-1]['close']
        exit_price = last_price
        reason = 'end_of_period'
        qty = position['qty']
        commission = qty * exit_price * TAKER_FEE
        if position['side'] == 'LONG':
            balance += qty * exit_price - commission
        else:
            balance -= qty * exit_price + commission
        pnl_total = balance - position['balance_before']
        events.append({
            'type': 'exit', 'idx': len(df) - 1, 'price': exit_price, 'trend': position['side'],
            'details': f"reason: {reason}, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
        })
        equity_curve.append(balance)

    total_trades = sum(1 for e in events if e['type'] == 'exit')
    profitable = 0
    losing = 0
    for e in events:
        if e['type'] != 'exit':
            continue
        details = e.get('details', '')
        if 'PnL:' not in details:
            continue
        try:
            pnl_val = float(details.split('PnL:')[1].split()[0])
        except (IndexError, ValueError):
            continue
        if pnl_val > 0:
            profitable += 1
        elif pnl_val < 0:
            losing += 1

    win_rate = (profitable / total_trades) if total_trades > 0 else 0.0
    net_pnl = balance - initial_balance

    max_dd_pct = 0.0
    if equity_curve:
        peak = equity_curve[0]
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100.0
                if dd > max_dd_pct:
                    max_dd_pct = dd

    result = {
        'final_balance': round(balance, 2), 'net_pnl': round(net_pnl, 2),
        'num_trades': total_trades, 'winning_trades': profitable, 'losing_trades': losing,
        'win_rate': round(win_rate, 4), 'max_drawdown_pct': round(max_dd_pct, 2),
        'events': events, 'equity_curve': equity_curve,
    }

    if verbose:
        print(f"\n=== ИТОГИ БЭКТЕСТА ===")
        print(f"Начальный баланс: {initial_balance:.2f} USDT")
        print(f"Конечный баланс: {result['final_balance']:.2f} USDT")
        print(f"Всего сделок: {total_trades}")
        if total_trades > 0:
            print(f"Прибыльных: {profitable}, Убыточных: {losing}")
            print(f"Win rate: {result['win_rate'] * 100:.1f}%")
            print(f"Max drawdown: {result['max_drawdown_pct']:.2f}%")
        print(f"Событий в логе: {len(events)}")
        print(f"MAX_LOSS_PER_TRADE (последнее): {params.get('MAX_LOSS_PER_TRADE')}")

    if save_events_path is not None:
        save_events(events, df, save_events_path)

    return result


def save_events(events, df, path):
    with open(path, 'w', encoding='utf-8') as f:
        f.write("timestamp,event,price,trend_type,details\n")
        for ev in events:
            if 'idx' in ev:
                ts = df.iloc[ev['idx']]['timestamp']
                price = f"{ev['price']:.2f}" if 'price' in ev else ''
                trend = ev.get('trend', '')
                details = ev.get('details', '')
                f.write(f"{ts},{ev['type']},{price},{trend},{details}\n")
            else:
                f.write(f",{ev['type']},,,{ev.get('details', '')}\n")
    print(f"Лог сохранён в {path}")

def main():
    try:
        df = pd.read_csv(INPUT_CSV)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.sort_values('timestamp', inplace=True)
        df.reset_index(drop=True, inplace=True)
        print(f"Загружено {len(df)} свечей из {INPUT_CSV}")
    except Exception as e:
        print(f"Ошибка загрузки данных: {e}")
        sys.exit(1)

    result = run_backtest(
        params=DEFAULT_PARAMS, df=df, initial_balance=1000.0,
        save_events_path=OUTPUT_LOG, verbose=True,
    )
    return result

if __name__ == "__main__":
    main()