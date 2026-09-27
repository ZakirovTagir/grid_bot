"""
find_swing_points.py
Бэктест стратегии на основе свинг-точек.

Рефакторинг (optimization branch):
- логика вынесена в функцию run_backtest(params, df);
- все параметры передаются через словарь params;
- добавлен расчёт max_drawdown_pct;
- при ручном запуске (__main__) поведение идентично прежней версии.
"""

import pandas as pd
import sys
from typing import Dict, Any, List, Optional


# ============================================================
#  Дефолтные параметры (для ручного запуска и как fallback)
# ============================================================
DEFAULT_PARAMS = {
    # --- группа A: структура ---
    'N': 7,
    'MIN_BODY_RATIO': 0.3,
    'DELTA_PRICE': 30,
    'MIN_DISTANCE_BARS': 5,
    'MAX_DELTA_EXTREMES': 500,
    # --- группа B: вход ---
    'ENTRY_TOLERANCE_USD': 50,
    'MIN_BARS_AFTER_POINT2': 3,
    'MAX_BARS_AFTER_POINT2': 7,
    # --- группа C: управление позицией ---
    'MAX_LOSS_PER_TRADE': 8.0,
    'MAX_STOP_DISTANCE_PERCENT': 2.5,
    'MAX_POSITION_PERCENT': 95.0,
    'MIN_POSITION_USDT': 10.0,
    'ACTIVATION_PROFIT_USD': 10.0,
    'BREAKEVEN_BARS_DELAY': 5,
    'BREAKEVEN_BIG_MOVE_USD': 100.0,
    'K1_MULT': 1.0,
    'K1_ADD': 150.0,
    'K2_MULT': 0.0,
    'K2_ADD': 0.0,
    'TRAILING_PRICE1': 0.0,
    'TRAILING_PRICE2': 0.0,
    'TRADING_FEE': 0.001,
}

# Пути для ручного запуска
INPUT_CSV = "data/historical/BTCUSDT_60.csv"
OUTPUT_LOG = "swing_points_log.txt"


# ============================================================
#  Вспомогательные функции
# ============================================================
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


# ============================================================
#  LongFinder
# ============================================================
class LongFinder:
    def __init__(self, df, params: Dict[str, Any]):
        self.df = df
        self.params = params
        self.state = "WAIT_MIN1"
        self.min1_price = None
        self.min1_idx = None
        self.max1_price = None
        self.max1_idx = None
        self.min2_price = None
        self.min2_idx = None
        self.pending_trend = None

    def reset(self):
        self.state = "WAIT_MIN1"
        self.min1_price = self.min1_idx = None
        self.max1_price = self.max1_idx = None
        self.min2_price = self.min2_idx = None
        self.pending_trend = None

    def process_block(self, block_start, block):
        if self.state == "WAIT_ENTRY":
            return

        N = self.params['N']
        min_body_ratio = self.params['MIN_BODY_RATIO']
        delta_price = self.params['DELTA_PRICE']
        min_distance = self.params['MIN_DISTANCE_BARS']
        max_delta = self.params['MAX_DELTA_EXTREMES']

        non_noisy = get_block_non_noisy(block, min_body_ratio)
        if not non_noisy:
            print(f"[LONG] Блок {block_start}-{block_start + N - 1}: все свечи шумные, пропуск.")
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)
        ts_L = self.df.iloc[L_idx]['timestamp']
        ts_H = self.df.iloc[H_idx]['timestamp']
        print(f"[LONG] Блок {block_start}-{block_start + N - 1}: L={L_price:.2f} ({ts_L}), H={H_price:.2f} ({ts_H})")

        if self.state == "WAIT_MIN1":
            if H_idx > L_idx and (H_price - L_price) >= delta_price:
                self.min1_price, self.min1_idx = L_price, L_idx
                self.max1_price, self.max1_idx = H_price, H_idx
                self.state = "WAIT_MIN2"
                print(f"[LONG] Найдены мин1 и макс1")
            else:
                print(f"[LONG] Условия не выполнены, сброс.")
                self.reset()
        elif self.state == "WAIT_MIN2":
            if H_price > self.max1_price:
                self.max1_price, self.max1_idx = H_price, H_idx
                print(f"[LONG] Обновлён макс1")
            if L_price > self.min1_price:
                distance = L_idx - self.min1_idx
                if distance >= min_distance and L_idx > self.max1_idx:
                    if max_delta > 0 and (L_price - self.min1_price) > max_delta:
                        print(f"[LONG] Разница мин2-мин1 > {max_delta}, сброс.")
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
                    print(f"[LONG] Найден мин2 – ожидание входа")
                else:
                    print(f"[LONG] Кандидат в мин2 не подходит, ждём дальше.")
            elif L_price < self.min1_price:
                print(f"[LONG] Перелом вниз, сброс.")
                self.reset()
            else:
                print(f"[LONG] L == мин1, сброс.")
                self.reset()

    def check_entry(self, current_idx, current_candle):
        if self.state != "WAIT_ENTRY":
            return None

        min_bars_after = self.params['MIN_BARS_AFTER_POINT2']
        max_bars_after = self.params['MAX_BARS_AFTER_POINT2']
        entry_tolerance = self.params['ENTRY_TOLERANCE_USD']

        if current_candle['low'] < self.min2_price:
            print(f"[LONG] Структура нарушена")
            self.reset()
            return None
        bars_since = current_idx - self.min2_idx
        if bars_since < min_bars_after:
            return None
        if bars_since > max_bars_after:
            print(f"[LONG] Таймаут входа")
            self.reset()
            return None

        t1, t2 = self.min1_idx, self.min2_idx
        support = self.min1_price + (self.min2_price - self.min1_price) * (current_idx - t1) / (t2 - t1)
        low = current_candle['low']
        high = current_candle['high']

        if low <= support + entry_tolerance and high >= support - entry_tolerance:
            channel_width = self.max1_price - self.min1_price
            structural_stop_distance = channel_width / 2.0
            return {
                'type': 'LONG',
                'entry_price': support,
                'entry_idx': current_idx,
                'min1': (self.min1_idx, self.min1_price),
                'max1': (self.max1_idx, self.max1_price),
                'min2': (self.min2_idx, self.min2_price),
                'structural_stop_distance': structural_stop_distance,
            }
        return None


# ============================================================
#  ShortFinder
# ============================================================
class ShortFinder:
    def __init__(self, df, params: Dict[str, Any]):
        self.df = df
        self.params = params
        self.state = "WAIT_MAX1"
        self.max1_price = None
        self.max1_idx = None
        self.min1_price = None
        self.min1_idx = None
        self.max2_price = None
        self.max2_idx = None
        self.pending_trend = None

    def reset(self):
        self.state = "WAIT_MAX1"
        self.max1_price = self.max1_idx = None
        self.min1_price = self.min1_idx = None
        self.max2_price = self.max2_idx = None
        self.pending_trend = None

    def process_block(self, block_start, block):
        if self.state == "WAIT_ENTRY":
            return

        N = self.params['N']
        min_body_ratio = self.params['MIN_BODY_RATIO']
        delta_price = self.params['DELTA_PRICE']
        min_distance = self.params['MIN_DISTANCE_BARS']
        max_delta = self.params['MAX_DELTA_EXTREMES']

        non_noisy = get_block_non_noisy(block, min_body_ratio)
        if not non_noisy:
            print(f"[SHORT] Блок {block_start}-{block_start + N - 1}: все свечи шумные, пропуск.")
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)
        ts_L = self.df.iloc[L_idx]['timestamp']
        ts_H = self.df.iloc[H_idx]['timestamp']
        print(f"[SHORT] Блок {block_start}-{block_start + N - 1}: L={L_price:.2f} ({ts_L}), H={H_price:.2f} ({ts_H})")

        if self.state == "WAIT_MAX1":
            if L_idx > H_idx and (H_price - L_price) >= delta_price:
                self.max1_price, self.max1_idx = H_price, H_idx
                self.min1_price, self.min1_idx = L_price, L_idx
                self.state = "WAIT_MAX2"
                print(f"[SHORT] Найдены макс1 и мин1")
            else:
                print(f"[SHORT] Условия не выполнены, сброс.")
                self.reset()
        elif self.state == "WAIT_MAX2":
            if L_price < self.min1_price:
                self.min1_price, self.min1_idx = L_price, L_idx
                print(f"[SHORT] Обновлён мин1")
            if H_price < self.max1_price:
                distance = H_idx - self.max1_idx
                if distance >= min_distance and H_idx > self.min1_idx:
                    if max_delta > 0 and (self.max1_price - H_price) > max_delta:
                        print(f"[SHORT] Разница макс1-макс2 > {max_delta}, сброс.")
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
                    print(f"[SHORT] Найден макс2 – ожидание входа")
                else:
                    print(f"[SHORT] Кандидат в макс2 не подходит, ждём дальше.")
            elif H_price > self.max1_price:
                print(f"[SHORT] Перелом вверх, сброс.")
                self.reset()
            else:
                print(f"[SHORT] H == макс1, сброс.")
                self.reset()

    def check_entry(self, current_idx, current_candle):
        if self.state != "WAIT_ENTRY":
            return None

        min_bars_after = self.params['MIN_BARS_AFTER_POINT2']
        max_bars_after = self.params['MAX_BARS_AFTER_POINT2']
        entry_tolerance = self.params['ENTRY_TOLERANCE_USD']

        if current_candle['high'] > self.max2_price:
            print(f"[SHORT] Структура нарушена")
            self.reset()
            return None
        bars_since = current_idx - self.max2_idx
        if bars_since < min_bars_after:
            return None
        if bars_since > max_bars_after:
            print(f"[SHORT] Таймаут входа")
            self.reset()
            return None

        t1, t2 = self.max1_idx, self.max2_idx
        resistance = self.max1_price + (self.max2_price - self.max1_price) * (current_idx - t1) / (t2 - t1)
        low = current_candle['low']
        high = current_candle['high']

        if low <= resistance + entry_tolerance and high >= resistance - entry_tolerance:
            channel_width = self.max1_price - self.min1_price
            structural_stop_distance = channel_width / 2.0
            return {
                'type': 'SHORT',
                'entry_price': resistance,
                'entry_idx': current_idx,
                'max1': (self.max1_idx, self.max1_price),
                'min1': (self.min1_idx, self.min1_price),
                'max2': (self.max2_idx, self.max2_price),
                'structural_stop_distance': structural_stop_distance,
            }
        return None


# ============================================================
#  Целевые уровни
# ============================================================
def get_active_levels(entry_price, side, structural_stop_distance, params):
    K1_MULT = params['K1_MULT']
    K1_ADD = params['K1_ADD']
    K2_MULT = params['K2_MULT']
    K2_ADD = params['K2_ADD']
    TRAILING_PRICE1 = params['TRAILING_PRICE1']
    TRAILING_PRICE2 = params['TRAILING_PRICE2']
    ACTIVATION_PROFIT_USD = params['ACTIVATION_PROFIT_USD']

    levels = []
    parts = [
        (K1_MULT, K1_ADD, TRAILING_PRICE1),
        (K2_MULT, K2_ADD, TRAILING_PRICE2),
    ]
    cumulative = entry_price

    for mult, add, fixed_price in parts:
        if fixed_price != 0:
            target = fixed_price
            cumulative = target
            levels.append(target)
        elif mult != 0 or add != 0:
            if side == 'LONG':
                if not levels:
                    target = entry_price + ACTIVATION_PROFIT_USD + structural_stop_distance * mult + add
                else:
                    target = cumulative + structural_stop_distance * mult + add
            else:
                if not levels:
                    target = entry_price - ACTIVATION_PROFIT_USD - structural_stop_distance * mult - add
                else:
                    target = cumulative - structural_stop_distance * mult - add
            cumulative = target
            levels.append(target)
        else:
            break
    return levels


# ============================================================
#  Ядро бэктеста
# ============================================================
def run_backtest(params: Dict[str, Any],
                 df: pd.DataFrame,
                 initial_balance: float = 1000.0,
                 save_events_path: Optional[str] = None,
                 verbose: bool = False) -> Dict[str, Any]:
    """
    Прогоняет стратегию на df с параметрами params.

    df должен иметь колонки: timestamp, open, high, low, close.
    Индексы df должны быть range(0, len(df)) — reset_index(drop=True) перед вызовом.

    Возвращает dict:
        final_balance, net_pnl, num_trades, winning_trades, losing_trades,
        win_rate, max_drawdown_pct, events, equity_curve
    """
    N = params['N']
    MAX_LOSS_PER_TRADE = params['MAX_LOSS_PER_TRADE']
    MAX_STOP_DISTANCE_PERCENT = params['MAX_STOP_DISTANCE_PERCENT']
    MAX_POSITION_PERCENT = params['MAX_POSITION_PERCENT']
    MIN_POSITION_USDT = params['MIN_POSITION_USDT']
    ACTIVATION_PROFIT_USD = params['ACTIVATION_PROFIT_USD']
    BREAKEVEN_BARS_DELAY = params['BREAKEVEN_BARS_DELAY']
    BREAKEVEN_BIG_MOVE_USD = params['BREAKEVEN_BIG_MOVE_USD']
    TRADING_FEE = params['TRADING_FEE']

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
            breakeven_reached = position['breakeven_reached']
            has_targets = position['has_targets']
            parts_active = position['parts_active']
            parts_qty = position['parts_qty']
            balance_before_open = position['balance_before']

            # ---- Безубыток ----
            if ACTIVATION_PROFIT_USD > 0 and not breakeven_reached:
                if side == 'LONG':
                    high = current_candle['high']
                    normal_trigger = (position['bars_since_entry'] >= BREAKEVEN_BARS_DELAY and
                                      high >= entry + ACTIVATION_PROFIT_USD)
                    big_move_trigger = (BREAKEVEN_BIG_MOVE_USD > 0 and
                                        high >= entry + BREAKEVEN_BIG_MOVE_USD)
                    if normal_trigger or big_move_trigger:
                        breakeven_reached = True
                        position['breakeven_reached'] = True
                        new_stop = entry + ACTIVATION_PROFIT_USD
                        if new_stop > stop_loss:
                            stop_loss = new_stop
                            position['stop_loss'] = stop_loss
                            events.append({'type': 'breakeven_activated', 'idx': i,
                                           'price': stop_loss, 'trend': 'LONG'})
                else:
                    low = current_candle['low']
                    normal_trigger = (position['bars_since_entry'] >= BREAKEVEN_BARS_DELAY and
                                      low <= entry - ACTIVATION_PROFIT_USD)
                    big_move_trigger = (BREAKEVEN_BIG_MOVE_USD > 0 and
                                        low <= entry - BREAKEVEN_BIG_MOVE_USD)
                    if normal_trigger or big_move_trigger:
                        breakeven_reached = True
                        position['breakeven_reached'] = True
                        new_stop = entry - ACTIVATION_PROFIT_USD
                        if new_stop < stop_loss:
                            stop_loss = new_stop
                            position['stop_loss'] = stop_loss
                            events.append({'type': 'breakeven_activated', 'idx': i,
                                           'price': stop_loss, 'trend': 'SHORT'})

            # ---- Тейки и стоп ----
            if side == 'LONG':
                high = current_candle['high']
                low = current_candle['low']

                if has_targets:
                    closed_parts = []
                    for part_idx, target_price in enumerate(parts_active):
                        if high >= target_price:
                            part_pnl = (target_price - entry) * parts_qty
                            commission = target_price * parts_qty * TRADING_FEE
                            net_pnl = part_pnl - commission
                            balance += parts_qty * target_price - commission
                            events.append({
                                'type': f'exit_part{part_idx + 1}', 'idx': i,
                                'price': target_price, 'trend': 'LONG',
                                'details': f"reason: target, PnL:{net_pnl:.2f} USDT, баланс:{balance:.2f}"
                            })
                            closed_parts.append(part_idx)
                    for pidx in sorted(closed_parts, reverse=True):
                        del parts_active[pidx]

                    if not parts_active:
                        pnl_total = balance - balance_before_open
                        events.append({
                            'type': 'exit', 'idx': i, 'price': target_price, 'trend': 'LONG',
                            'details': f"reason: all_targets_closed, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
                        })
                        equity_curve.append(balance)
                        position = None
                        long_finder.reset()
                        short_finder.reset()
                        buffer.clear()
                        block_start = i + 1
                        i += 1
                        continue

                if low <= stop_loss:
                    exit_price = stop_loss
                    if has_targets:
                        total_remaining_qty = len(parts_active) * parts_qty
                    else:
                        total_remaining_qty = qty
                    reason = 'breakeven_stop' if breakeven_reached else 'stop_loss'
                    commission = total_remaining_qty * exit_price * TRADING_FEE
                    balance += total_remaining_qty * exit_price - commission
                    pnl_total = balance - balance_before_open
                    events.append({
                        'type': 'exit', 'idx': i, 'price': exit_price, 'trend': 'LONG',
                        'details': f"reason: {reason}, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
                    })
                    equity_curve.append(balance)
                    position = None
                    long_finder.reset()
                    short_finder.reset()
                    buffer.clear()
                    block_start = i + 1
                    i += 1
                    continue

            else:  # SHORT
                high = current_candle['high']
                low = current_candle['low']

                if has_targets:
                    closed_parts = []
                    for part_idx, target_price in enumerate(parts_active):
                        if low <= target_price:
                            part_pnl = (entry - target_price) * parts_qty
                            commission = target_price * parts_qty * TRADING_FEE
                            net_pnl = part_pnl - commission
                            balance -= parts_qty * target_price + commission
                            events.append({
                                'type': f'exit_part{part_idx + 1}', 'idx': i,
                                'price': target_price, 'trend': 'SHORT',
                                'details': f"reason: target, PnL:{net_pnl:.2f} USDT, баланс:{balance:.2f}"
                            })
                            closed_parts.append(part_idx)
                    for pidx in sorted(closed_parts, reverse=True):
                        del parts_active[pidx]

                    if not parts_active:
                        pnl_total = balance - balance_before_open
                        events.append({
                            'type': 'exit', 'idx': i, 'price': target_price, 'trend': 'SHORT',
                            'details': f"reason: all_targets_closed, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
                        })
                        equity_curve.append(balance)
                        position = None
                        long_finder.reset()
                        short_finder.reset()
                        buffer.clear()
                        block_start = i + 1
                        i += 1
                        continue

                if high >= stop_loss:
                    exit_price = stop_loss
                    if has_targets:
                        total_remaining_qty = len(parts_active) * parts_qty
                    else:
                        total_remaining_qty = qty
                    reason = 'breakeven_stop' if breakeven_reached else 'stop_loss'
                    commission = total_remaining_qty * exit_price * TRADING_FEE
                    balance -= total_remaining_qty * exit_price + commission
                    pnl_total = balance - balance_before_open
                    events.append({
                        'type': 'exit', 'idx': i, 'price': exit_price, 'trend': 'SHORT',
                        'details': f"reason: {reason}, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
                    })
                    equity_curve.append(balance)
                    position = None
                    long_finder.reset()
                    short_finder.reset()
                    buffer.clear()
                    block_start = i + 1
                    i += 1
                    continue

        else:  # нет позиции — ищем вход
            buffer.append(i)
            if len(buffer) == N:
                block = df.iloc[buffer]
                long_finder.process_block(block_start, block)
                short_finder.process_block(block_start, block)
                buffer.clear()
                block_start = i + 1

            for finder, _side_label in [(long_finder, 'LONG'), (short_finder, 'SHORT')]:
                signal = finder.check_entry(i, current_candle)
                if signal is not None:
                    entry_price = signal['entry_price']
                    structural_stop_distance = signal['structural_stop_distance']

                    if signal['type'] == 'LONG':
                        structural_stop = entry_price - structural_stop_distance
                    else:
                        structural_stop = entry_price + structural_stop_distance

                    max_allowed_stop_dist = entry_price * MAX_STOP_DISTANCE_PERCENT / 100
                    if structural_stop_distance <= max_allowed_stop_dist:
                        stop_loss = structural_stop
                        actual_stop_distance = structural_stop_distance
                    else:
                        if signal['type'] == 'LONG':
                            stop_loss = entry_price - max_allowed_stop_dist
                        else:
                            stop_loss = entry_price + max_allowed_stop_dist
                        actual_stop_distance = max_allowed_stop_dist

                    qty = MAX_LOSS_PER_TRADE / actual_stop_distance
                    position_value = qty * entry_price

                    if position_value < MIN_POSITION_USDT:
                        qty = MIN_POSITION_USDT / entry_price
                        max_stop_distance = MAX_LOSS_PER_TRADE / qty if qty > 0 else 0
                        new_stop_distance = min(actual_stop_distance, max_stop_distance)
                        if signal['type'] == 'LONG':
                            stop_loss = entry_price - new_stop_distance
                        else:
                            stop_loss = entry_price + new_stop_distance
                        actual_stop_distance = new_stop_distance
                        qty = MAX_LOSS_PER_TRADE / actual_stop_distance
                        position_value = qty * entry_price

                    max_position_usdt = balance * MAX_POSITION_PERCENT / 100
                    if position_value > max_position_usdt:
                        position_value = max_position_usdt
                        qty = position_value / entry_price
                        if qty > 0:
                            actual_stop_distance = MAX_LOSS_PER_TRADE / qty
                            if signal['type'] == 'LONG':
                                stop_loss = entry_price - actual_stop_distance
                            else:
                                stop_loss = entry_price + actual_stop_distance

                    if position_value < MIN_POSITION_USDT:
                        events.append({
                            'type': 'entry_cancelled',
                            'details': f"малая позиция ({position_value:.2f} < {MIN_POSITION_USDT})",
                            'trend': signal['type']
                        })
                        finder.reset()
                        continue

                    commission_open = position_value * TRADING_FEE

                    target_levels = get_active_levels(entry_price, signal['type'],
                                                      structural_stop_distance, params)
                    num_parts = len(target_levels)
                    has_targets = num_parts > 0
                    parts_qty = qty / num_parts if has_targets else qty

                    expected_profit = 0.0
                    if has_targets:
                        if signal['type'] == 'LONG':
                            for target in target_levels:
                                expected_profit += (target - entry_price) * parts_qty
                        else:
                            for target in target_levels:
                                expected_profit += (entry_price - target) * parts_qty

                    balance_before = balance

                    if signal['type'] == 'LONG':
                        balance -= position_value + commission_open
                    else:
                        balance += position_value - commission_open

                    position = {
                        'side': signal['type'],
                        'entry_price': entry_price,
                        'qty': qty,
                        'stop_loss': stop_loss,
                        'breakeven_reached': False,
                        'has_targets': has_targets,
                        'parts_active': target_levels.copy() if has_targets else [],
                        'parts_qty': parts_qty,
                        'balance_before': balance_before,
                        'entry_idx': i,
                        'bars_since_entry': 0,
                    }

                    trend_points = finder.pending_trend
                    if trend_points:
                        if trend_points['type'] == 'LONG':
                            events.append({'type': 'мин1', 'idx': trend_points['min1'][0],
                                           'price': trend_points['min1'][1], 'trend': 'LONG'})
                            events.append({'type': 'макс1', 'idx': trend_points['max1'][0],
                                           'price': trend_points['max1'][1], 'trend': 'LONG'})
                            events.append({'type': 'мин2', 'idx': trend_points['min2'][0],
                                           'price': trend_points['min2'][1], 'trend': 'LONG'})
                        else:
                            events.append({'type': 'макс1', 'idx': trend_points['max1'][0],
                                           'price': trend_points['max1'][1], 'trend': 'SHORT'})
                            events.append({'type': 'мин1', 'idx': trend_points['min1'][0],
                                           'price': trend_points['min1'][1], 'trend': 'SHORT'})
                            events.append({'type': 'макс2', 'idx': trend_points['max2'][0],
                                           'price': trend_points['max2'][1], 'trend': 'SHORT'})

                    events.append({
                        'type': 'entry', 'idx': i, 'price': entry_price, 'trend': signal['type'],
                        'details': f"stop_loss:{stop_loss:.2f}, qty:{qty:.6f}, "
                                   f"pos_value:{position_value:.2f}, parts:{num_parts}, "
                                   f"expected_profit:{expected_profit:.2f} USDT"
                    })

                    finder.reset()
                    break

        i += 1

    # Закрытие позиции в конце периода
    if position is not None:
        last_price = df.iloc[-1]['close']
        if position['side'] == 'LONG':
            exit_price = last_price
            reason = 'end_of_period'
            if position['has_targets']:
                total_qty = len(position['parts_active']) * position['parts_qty']
            else:
                total_qty = position['qty']
            commission = total_qty * exit_price * TRADING_FEE
            balance += total_qty * exit_price - commission
        else:
            exit_price = last_price
            reason = 'end_of_period'
            if position['has_targets']:
                total_qty = len(position['parts_active']) * position['parts_qty']
            else:
                total_qty = position['qty']
            commission = total_qty * exit_price * TRADING_FEE
            balance -= total_qty * exit_price + commission
        pnl_total = balance - position['balance_before']
        events.append({
            'type': 'exit', 'idx': len(df) - 1, 'price': exit_price, 'trend': position['side'],
            'details': f"reason: {reason}, PnL:{pnl_total:.2f} USDT, баланс:{balance:.2f}"
        })
        equity_curve.append(balance)

    # ---- Считаем метрики ----
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

    # ---- Max drawdown ----
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
        'final_balance': round(balance, 2),
        'net_pnl': round(net_pnl, 2),
        'num_trades': total_trades,
        'winning_trades': profitable,
        'losing_trades': losing,
        'win_rate': round(win_rate, 4),
        'max_drawdown_pct': round(max_dd_pct, 2),
        'events': events,
        'equity_curve': equity_curve,
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

    if save_events_path is not None:
        save_events(events, df, save_events_path)
        print(f"Лог сохранён в {save_events_path}")

    return result


# ============================================================
#  Сохранение событий
# ============================================================
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


# ============================================================
#  Ручной запуск
# ============================================================
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
        params=DEFAULT_PARAMS,
        df=df,
        initial_balance=1000.0,
        save_events_path=OUTPUT_LOG,
        verbose=True,
    )
    return result


if __name__ == "__main__":
    main()