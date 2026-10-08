"""
core/swing_finder.py
Процедурный поиск свинг-точек с детальным логированием.

v4:
- [PATCH 15] Лог при отказе кандидата в мин2/макс2 показывает обе проверки:
  distance >= min_distance и idx > ref_idx. Раньше писалась только дистанция,
  что путало (писало «дистанция N», хотя N проходило, а валился idx).

v3:
- MAX_DELTA_EXTREMES теперь безразмерный множитель k.
- process_block принимает buffer_df для доступа к телам свечей.
"""
import logging
import pandas as pd

logger = logging.getLogger(__name__)


def is_noisy(row, min_body_ratio=0.3):
    high = row['high']
    low = row['low']
    open_ = row['open']
    close = row['close']
    candle_range = high - low
    if candle_range <= 0:
        return True
    body = abs(close - open_)
    return (body / candle_range) < min_body_ratio


def get_block_extremes(block, min_body_ratio=0.3):
    non_noisy = []
    for idx, row in block.iterrows():
        if not is_noisy(row, min_body_ratio):
            non_noisy.append((idx, row))
    if not non_noisy:
        return None, None
    min_row = min(non_noisy, key=lambda x: (x[1]['low'], x[0]))
    max_row = max(non_noisy, key=lambda x: (x[1]['high'], -x[0]))
    return (min_row[1]['low'], min_row[0]), (max_row[1]['high'], max_row[0])


def _candle_body(buffer_df: pd.DataFrame, idx: int) -> float:
    row = buffer_df.iloc[idx]
    return abs(float(row['close']) - float(row['open']))


def _avg_body_between(buffer_df: pd.DataFrame, idx_start: int, idx_end: int) -> float:
    if idx_end < idx_start:
        return 0.0
    bodies = [_candle_body(buffer_df, i) for i in range(idx_start, idx_end + 1)]
    if not bodies:
        return 0.0
    return sum(bodies) / len(bodies)


def _check_body_spike(buffer_df: pd.DataFrame, idx_point1: int, idx_point2: int,
                      k: float) -> bool:
    if k <= 0:
        return False
    body_p2 = _candle_body(buffer_df, idx_point2)
    avg_body = _avg_body_between(buffer_df, idx_point1, idx_point2)
    if avg_body <= 0:
        return False
    ratio = body_p2 / avg_body
    if ratio > k:
        logger.info(
            f"body_spike: точка2 idx={idx_point2}, body={body_p2:.2f}, "
            f"avg_body={avg_body:.2f}, ratio={ratio:.2f} > k={k}"
        )
        return True
    return False


def process_block(state, block_start, block, params, buffer_df=None):
    delta_price = params.get('DELTA_PRICE', 30)
    min_distance = params.get('MIN_DISTANCE_BARS', 5)
    max_delta_k = params.get('MAX_DELTA_EXTREMES', 4.0)
    min_body_ratio = params.get('MIN_BODY_RATIO', 0.3)

    logger.info(f"Обработка блока {block_start}-{block_start + 6}")
    extremes = get_block_extremes(block, min_body_ratio)
    if extremes[0] is None:
        logger.info(f"Блок {block_start}: все свечи шумные")
        return
    (L_price, L_idx), (H_price, H_idx) = extremes
    logger.info(f"Блок экстремумы: L={L_price} (idx {L_idx}), H={H_price} (idx {H_idx})")
    delta = H_price - L_price
    logger.info(f"Дельта H-L = {delta}, требуется >= {delta_price}")

    # --- LONG ---
    if state['long']['state'] != 'WAIT_ENTRY':
        if state['long']['state'] == 'WAIT_MIN1':
            if H_idx > L_idx and delta >= delta_price:
                state['long']['min1'] = (L_idx, L_price)
                state['long']['max1'] = (H_idx, H_price)
                state['long']['state'] = 'WAIT_MIN2'
                logger.info(f"LONG: переход в WAIT_MIN2, мин1={L_price} (idx {L_idx}), макс1={H_price} (idx {H_idx})")
            else:
                logger.info(f"LONG: условия WAIT_MIN1 не выполнены")
        elif state['long']['state'] == 'WAIT_MIN2':
            if H_price > state['long']['max1'][1]:
                state['long']['max1'] = (H_idx, H_price)
                logger.info(f"LONG: обновлён макс1 до {H_price} (idx {H_idx})")
            if L_price > state['long']['min1'][1]:
                distance = L_idx - state['long']['min1'][0]
                ref_idx = state['long']['max1'][0]
                ok_dist = distance >= min_distance
                ok_idx = L_idx > ref_idx
                logger.info(
                    f"LONG: кандидат в мин2: L={L_price} (idx {L_idx}), "
                    f"distance={distance}>={min_distance}? {ok_dist}; "
                    f"L_idx={L_idx} > max1_idx={ref_idx}? {ok_idx}"
                )
                if ok_dist and ok_idx:
                    if buffer_df is not None and _check_body_spike(
                        buffer_df, state['long']['min1'][0], L_idx, max_delta_k
                    ):
                        logger.info(f"LONG: body_spike отбросил структуру, сброс")
                        state['long']['state'] = 'WAIT_MIN1'
                        state['long']['min1'] = state['long']['max1'] = None
                    else:
                        state['long']['min2'] = (L_idx, L_price)
                        state['long']['state'] = 'WAIT_ENTRY'
                        logger.info(f"LONG: переход в WAIT_ENTRY, мин2={L_price} (idx {L_idx})")
                else:
                    logger.info(
                        f"LONG: кандидат в мин2 отклонён "
                        f"(ok_dist={ok_dist}, ok_idx={ok_idx})"
                    )
            elif L_price < state['long']['min1'][1]:
                logger.info("LONG: перелом вниз, сброс")
                state['long']['state'] = 'WAIT_MIN1'
                state['long']['min1'] = state['long']['max1'] = None
            else:
                logger.info("LONG: L == мин1, сброс")
                state['long']['state'] = 'WAIT_MIN1'
                state['long']['min1'] = state['long']['max1'] = None

    # --- SHORT ---
    if state['short']['state'] != 'WAIT_ENTRY':
        if state['short']['state'] == 'WAIT_MAX1':
            if L_idx > H_idx and delta >= delta_price:
                state['short']['max1'] = (H_idx, H_price)
                state['short']['min1'] = (L_idx, L_price)
                state['short']['state'] = 'WAIT_MAX2'
                logger.info(f"SHORT: переход в WAIT_MAX2, макс1={H_price} (idx {H_idx}), мин1={L_price} (idx {L_idx})")
            else:
                logger.info(f"SHORT: условия WAIT_MAX1 не выполнены")
        elif state['short']['state'] == 'WAIT_MAX2':
            if L_price < state['short']['min1'][1]:
                state['short']['min1'] = (L_idx, L_price)
                logger.info(f"SHORT: обновлён мин1 до {L_price} (idx {L_idx})")
            if H_price < state['short']['max1'][1]:
                distance = H_idx - state['short']['max1'][0]
                ref_idx = state['short']['min1'][0]
                ok_dist = distance >= min_distance
                ok_idx = H_idx > ref_idx
                logger.info(
                    f"SHORT: кандидат в макс2: H={H_price} (idx {H_idx}), "
                    f"distance={distance}>={min_distance}? {ok_dist}; "
                    f"H_idx={H_idx} > min1_idx={ref_idx}? {ok_idx}"
                )
                if ok_dist and ok_idx:
                    if buffer_df is not None and _check_body_spike(
                        buffer_df, state['short']['max1'][0], H_idx, max_delta_k
                    ):
                        logger.info(f"SHORT: body_spike отбросил структуру, сброс")
                        state['short']['state'] = 'WAIT_MAX1'
                        state['short']['max1'] = state['short']['min1'] = None
                    else:
                        state['short']['max2'] = (H_idx, H_price)
                        state['short']['state'] = 'WAIT_ENTRY'
                        logger.info(f"SHORT: переход в WAIT_ENTRY, макс2={H_price} (idx {H_idx})")
                else:
                    logger.info(
                        f"SHORT: кандидат в макс2 отклонён "
                        f"(ok_dist={ok_dist}, ok_idx={ok_idx})"
                    )
            elif H_price > state['short']['max1'][1]:
                logger.info("SHORT: перелом вверх, сброс")
                state['short']['state'] = 'WAIT_MAX1'
                state['short']['max1'] = state['short']['min1'] = None
            else:
                logger.info("SHORT: H == макс1, сброс")
                state['short']['state'] = 'WAIT_MAX1'
                state['short']['max1'] = state['short']['min1'] = None


def check_entry(state, current_idx, current_candle, params):
    min_bars_after = params.get('MIN_BARS_AFTER_POINT2', 3)
    max_bars_after = params.get('MAX_BARS_AFTER_POINT2', 10)
    entry_tolerance = params.get('ENTRY_TOLERANCE_USD', 50)

    # --- LONG ---
    if state['long']['state'] == 'WAIT_ENTRY':
        long = state['long']
        logger.info(f"LONG check_entry: свеча {current_idx}, state WAIT_ENTRY")
        if current_candle['low'] < long['min2'][1]:
            logger.info(f"LONG: структура нарушена")
            state['long']['state'] = 'WAIT_MIN1'
            state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
            return None
        bars_since = current_idx - long['min2'][0]
        logger.info(f"LONG: баров с момента мин2 = {bars_since}, допустимо {min_bars_after}-{max_bars_after}")
        if bars_since < min_bars_after:
            return None
        if bars_since > max_bars_after:
            logger.info(f"LONG: таймаут")
            state['long']['state'] = 'WAIT_MIN1'
            state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
            return None
        t1, t2 = long['min1'][0], long['min2'][0]
        p1, p2 = long['min1'][1], long['min2'][1]
        support = p1 + (p2 - p1) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']
        if low <= support + entry_tolerance and high >= support - entry_tolerance:
            logger.info(f"LONG: СИГНАЛ ВХОДА, цена {support:.2f}")
            return {
                'type': 'LONG',
                'entry_price': support,
                'entry_idx': current_idx,
                'min1': long['min1'],
                'max1': long['max1'],
                'min2': long['min2'],
            }
        return None

    # --- SHORT ---
    if state['short']['state'] == 'WAIT_ENTRY':
        short = state['short']
        logger.info(f"SHORT check_entry: свеча {current_idx}, state WAIT_ENTRY")
        if current_candle['high'] > short['max2'][1]:
            logger.info(f"SHORT: структура нарушена")
            state['short']['state'] = 'WAIT_MAX1'
            state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None
            return None
        bars_since = current_idx - short['max2'][0]
        logger.info(f"SHORT: баров с момента макс2 = {bars_since}, допустимо {min_bars_after}-{max_bars_after}")
        if bars_since < min_bars_after:
            return None
        if bars_since > max_bars_after:
            logger.info(f"SHORT: таймаут")
            state['short']['state'] = 'WAIT_MAX1'
            state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None
            return None
        t1, t2 = short['max1'][0], short['max2'][0]
        p1, p2 = short['max1'][1], short['max2'][1]
        resistance = p1 + (p2 - p1) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']
        if low <= resistance + entry_tolerance and high >= resistance - entry_tolerance:
            logger.info(f"SHORT: СИГНАЛ ВХОДА, цена {resistance:.2f}")
            return {
                'type': 'SHORT',
                'entry_price': resistance,
                'entry_idx': current_idx,
                'max1': short['max1'],
                'min1': short['min1'],
                'max2': short['max2'],
            }
        return None

    return None