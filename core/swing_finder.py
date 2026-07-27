"""
core/swing_finder.py
Процедурный поиск свинг-точек с детальным логированием.
"""
import logging
logger = logging.getLogger(__name__)

def is_noisy(row, min_body_ratio=0.3):
    high, low, open_, close = row['high'], row['low'], row['open'], row['close']
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

def process_block(state, block_start, block, params):
    delta_price = params.get('DELTA_PRICE', 30)
    min_distance = params.get('MIN_DISTANCE_BARS', 5)
    max_delta = params.get('MAX_DELTA_EXTREMES', 500)
    min_body_ratio = params.get('MIN_BODY_RATIO', 0.3)

    logger.info(f"Обработка блока {block_start}-{block_start+6}")
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
                logger.info(f"LONG: условия WAIT_MIN1 не выполнены (H_idx={H_idx} > L_idx={L_idx}? {H_idx > L_idx}, delta={delta} >= {delta_price}? {delta >= delta_price})")
        elif state['long']['state'] == 'WAIT_MIN2':
            if H_price > state['long']['max1'][1]:
                state['long']['max1'] = (H_idx, H_price)
                logger.info(f"LONG: обновлён макс1 до {H_price} (idx {H_idx})")
            if L_price > state['long']['min1'][1]:
                distance = L_idx - state['long']['min1'][0]
                logger.info(f"LONG: кандидат в мин2: L={L_price} (idx {L_idx}), дистанция={distance}, требуется {min_distance}")
                if distance >= min_distance and L_idx > state['long']['max1'][0]:
                    if max_delta > 0 and (L_price - state['long']['min1'][1]) > max_delta:
                        logger.info(f"LONG: разница мин2-мин1 > {max_delta}, сброс")
                        state['long']['state'] = 'WAIT_MIN1'
                        state['long']['min1'] = state['long']['max1'] = None
                    else:
                        state['long']['min2'] = (L_idx, L_price)
                        state['long']['state'] = 'WAIT_ENTRY'
                        logger.info(f"LONG: переход в WAIT_ENTRY, мин2={L_price} (idx {L_idx})")
                else:
                    logger.info(f"LONG: кандидат в мин2 не подходит (дистанция {distance})")
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
                logger.info(f"SHORT: условия WAIT_MAX1 не выполнены (L_idx={L_idx} > H_idx={H_idx}? {L_idx > H_idx}, delta={delta} >= {delta_price}? {delta >= delta_price})")
        elif state['short']['state'] == 'WAIT_MAX2':
            if L_price < state['short']['min1'][1]:
                state['short']['min1'] = (L_idx, L_price)
                logger.info(f"SHORT: обновлён мин1 до {L_price} (idx {L_idx})")
            if H_price < state['short']['max1'][1]:
                distance = H_idx - state['short']['max1'][0]
                logger.info(f"SHORT: кандидат в макс2: H={H_price} (idx {H_idx}), дистанция={distance}, требуется {min_distance}")
                if distance >= min_distance and H_idx > state['short']['min1'][0]:
                    if max_delta > 0 and (state['short']['max1'][1] - H_price) > max_delta:
                        logger.info(f"SHORT: разница макс1-макс2 > {max_delta}, сброс")
                        state['short']['state'] = 'WAIT_MAX1'
                        state['short']['max1'] = state['short']['min1'] = None
                    else:
                        state['short']['max2'] = (H_idx, H_price)
                        state['short']['state'] = 'WAIT_ENTRY'
                        logger.info(f"SHORT: переход в WAIT_ENTRY, макс2={H_price} (idx {H_idx})")
                else:
                    logger.info(f"SHORT: кандидат в макс2 не подходит (дистанция {distance})")
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
    max_bars_after = params.get('MAX_BARS_AFTER_POINT2', 7)
    entry_tolerance = params.get('ENTRY_TOLERANCE_USD', 50)

    # --- LONG ---
    if state['long']['state'] == 'WAIT_ENTRY':
        long = state['long']
        logger.info(f"LONG check_entry: свеча {current_idx}, state WAIT_ENTRY")
        if current_candle['low'] < long['min2'][1]:
            logger.info(f"LONG: структура нарушена (low={current_candle['low']} < min2={long['min2'][1]})")
            state['long']['state'] = 'WAIT_MIN1'
            state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
            return None
        bars_since = current_idx - long['min2'][0]
        logger.info(f"LONG: баров с момента мин2 = {bars_since}, допустимо {min_bars_after}-{max_bars_after}")
        if bars_since < min_bars_after:
            logger.info(f"LONG: слишком рано (прошло {bars_since} баров)")
            return None
        if bars_since > max_bars_after:
            logger.info(f"LONG: таймаут (прошло {bars_since} баров)")
            state['long']['state'] = 'WAIT_MIN1'
            state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
            return None
        t1, t2 = long['min1'][0], long['min2'][0]
        p1, p2 = long['min1'][1], long['min2'][1]
        support = p1 + (p2 - p1) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']
        logger.info(f"LONG: линия тренда support={support:.2f}, low={low}, high={high}, tolerance={entry_tolerance}")
        if low <= support + entry_tolerance and high >= support - entry_tolerance:
            logger.info(f"LONG: СИГНАЛ ВХОДА, цена {support:.2f}")
            return {
                'type': 'LONG',
                'entry_price': support,
                'entry_idx': current_idx,
                'min1': long['min1'],
                'max1': long['max1'],
                'min2': long['min2']
            }
        else:
            logger.info(f"LONG: цена не подходит (low={low}, high={high}, support={support})")
            return None
    else:
        logger.debug(f"LONG: состояние {state['long']['state']}, check_entry пропущен")

    # --- SHORT ---
    if state['short']['state'] == 'WAIT_ENTRY':
        short = state['short']
        logger.info(f"SHORT check_entry: свеча {current_idx}, state WAIT_ENTRY")
        if current_candle['high'] > short['max2'][1]:
            logger.info(f"SHORT: структура нарушена (high={current_candle['high']} > max2={short['max2'][1]})")
            state['short']['state'] = 'WAIT_MAX1'
            state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None
            return None
        bars_since = current_idx - short['max2'][0]
        logger.info(f"SHORT: баров с момента макс2 = {bars_since}, допустимо {min_bars_after}-{max_bars_after}")
        if bars_since < min_bars_after:
            logger.info(f"SHORT: слишком рано (прошло {bars_since} баров)")
            return None
        if bars_since > max_bars_after:
            logger.info(f"SHORT: таймаут (прошло {bars_since} баров)")
            state['short']['state'] = 'WAIT_MAX1'
            state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None
            return None
        t1, t2 = short['max1'][0], short['max2'][0]
        p1, p2 = short['max1'][1], short['max2'][1]
        resistance = p1 + (p2 - p1) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']
        logger.info(f"SHORT: линия тренда resistance={resistance:.2f}, low={low}, high={high}, tolerance={entry_tolerance}")
        if low <= resistance + entry_tolerance and high >= resistance - entry_tolerance:
            logger.info(f"SHORT: СИГНАЛ ВХОДА, цена {resistance:.2f}")
            return {
                'type': 'SHORT',
                'entry_price': resistance,
                'entry_idx': current_idx,
                'max1': short['max1'],
                'min1': short['min1'],
                'max2': short['max2']
            }
        else:
            logger.info(f"SHORT: цена не подходит (low={low}, high={high}, resistance={resistance})")
            return None
    else:
        logger.debug(f"SHORT: состояние {state['short']['state']}, check_entry пропущен")
    return None