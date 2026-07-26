"""
Процедурный поиск свинг-точек (без классов).
Используется в боте для замены LongFinder/ShortFinder.
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
    """
    Обрабатывает один непересекающийся блок, обновляя состояние.
    state: {'long': {...}, 'short': {...}}
    """
    delta_price = params.get('DELTA_PRICE', 30)
    min_distance = params.get('MIN_DISTANCE_BARS', 5)
    max_delta = params.get('MAX_DELTA_EXTREMES', 500)
    min_body_ratio = params.get('MIN_BODY_RATIO', 0.3)

    # --- LONG ---
    if state['long']['state'] != 'WAIT_ENTRY':
        extremes = get_block_extremes(block, min_body_ratio)
        if extremes[0] is not None:
            (L_price, L_idx), (H_price, H_idx) = extremes
            if state['long']['state'] == 'WAIT_MIN1':
                if H_idx > L_idx and (H_price - L_price) >= delta_price:
                    state['long']['min1'] = (L_idx, L_price)
                    state['long']['max1'] = (H_idx, H_price)
                    state['long']['state'] = 'WAIT_MIN2'
                    logger.debug(f"LONG: найдены мин1={L_price} (idx {L_idx}), макс1={H_price} (idx {H_idx})")
                else:
                    logger.debug(f"LONG: условия WAIT_MIN1 не выполнены (delta={H_price-L_price})")
            elif state['long']['state'] == 'WAIT_MIN2':
                if H_price > state['long']['max1'][1]:
                    state['long']['max1'] = (H_idx, H_price)
                if L_price > state['long']['min1'][1]:
                    distance = L_idx - state['long']['min1'][0]
                    if distance >= min_distance and L_idx > state['long']['max1'][0]:
                        if max_delta > 0 and (L_price - state['long']['min1'][1]) > max_delta:
                            logger.debug(f"LONG: разница мин2-мин1 > {max_delta}, сброс")
                            state['long']['state'] = 'WAIT_MIN1'
                            state['long']['min1'] = state['long']['max1'] = None
                        else:
                            state['long']['min2'] = (L_idx, L_price)
                            state['long']['state'] = 'WAIT_ENTRY'
                            logger.info(f"LONG: найден мин2={L_price} (idx {L_idx}) – переход в WAIT_ENTRY")
                    else:
                        logger.debug(f"LONG: кандидат в мин2 не подходит (дистанция {distance})")
                elif L_price < state['long']['min1'][1]:
                    logger.debug("LONG: перелом вниз, сброс")
                    state['long']['state'] = 'WAIT_MIN1'
                    state['long']['min1'] = state['long']['max1'] = None

    # --- SHORT ---
    if state['short']['state'] != 'WAIT_ENTRY':
        extremes = get_block_extremes(block, min_body_ratio)
        if extremes[0] is not None:
            (L_price, L_idx), (H_price, H_idx) = extremes
            if state['short']['state'] == 'WAIT_MAX1':
                if L_idx > H_idx and (H_price - L_price) >= delta_price:
                    state['short']['max1'] = (H_idx, H_price)
                    state['short']['min1'] = (L_idx, L_price)
                    state['short']['state'] = 'WAIT_MAX2'
                    logger.debug(f"SHORT: найдены макс1={H_price} (idx {H_idx}), мин1={L_price} (idx {L_idx})")
                else:
                    logger.debug(f"SHORT: условия WAIT_MAX1 не выполнены (delta={H_price-L_price})")
            elif state['short']['state'] == 'WAIT_MAX2':
                if L_price < state['short']['min1'][1]:
                    state['short']['min1'] = (L_idx, L_price)
                if H_price < state['short']['max1'][1]:
                    distance = H_idx - state['short']['max1'][0]
                    if distance >= min_distance and H_idx > state['short']['min1'][0]:
                        if max_delta > 0 and (state['short']['max1'][1] - H_price) > max_delta:
                            logger.debug(f"SHORT: разница макс1-макс2 > {max_delta}, сброс")
                            state['short']['state'] = 'WAIT_MAX1'
                            state['short']['max1'] = state['short']['min1'] = None
                        else:
                            state['short']['max2'] = (H_idx, H_price)
                            state['short']['state'] = 'WAIT_ENTRY'
                            logger.info(f"SHORT: найден макс2={H_price} (idx {H_idx}) – переход в WAIT_ENTRY")
                    else:
                        logger.debug(f"SHORT: кандидат в макс2 не подходит (дистанция {distance})")
                elif H_price > state['short']['max1'][1]:
                    logger.debug("SHORT: перелом вверх, сброс")
                    state['short']['state'] = 'WAIT_MAX1'
                    state['short']['max1'] = state['short']['min1'] = None

def check_entry(state, current_idx, current_candle, params):
    """
    Проверяет вход на текущей свече для LONG и SHORT.
    Возвращает сигнал (dict) или None.
    """
    min_bars_after = params.get('MIN_BARS_AFTER_POINT2', 3)
    max_bars_after = params.get('MAX_BARS_AFTER_POINT2', 7)
    entry_tolerance = params.get('ENTRY_TOLERANCE_USD', 50)

    # --- LONG ---
    if state['long']['state'] == 'WAIT_ENTRY':
        long = state['long']
        if current_candle['low'] < long['min2'][1]:
            logger.debug("LONG: структура нарушена (low < min2)")
            state['long']['state'] = 'WAIT_MIN1'
            state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
        else:
            bars_since = current_idx - long['min2'][0]
            if bars_since < min_bars_after:
                logger.debug(f"LONG: слишком рано (прошло {bars_since} баров)")
            elif bars_since > max_bars_after:
                logger.debug(f"LONG: таймаут (прошло {bars_since} баров)")
                state['long']['state'] = 'WAIT_MIN1'
                state['long']['min1'] = state['long']['max1'] = state['long']['min2'] = None
            else:
                t1, t2 = long['min1'][0], long['min2'][0]
                p1, p2 = long['min1'][1], long['min2'][1]
                support = p1 + (p2 - p1) * (current_idx - t1) / (t2 - t1)
                low, high = current_candle['low'], current_candle['high']
                if low <= support + entry_tolerance and high >= support - entry_tolerance:
                    logger.info(f"LONG: СИГНАЛ ВХОДА на свече {current_idx}, цена {support:.2f}")
                    return {
                        'type': 'LONG',
                        'entry_price': support,
                        'entry_idx': current_idx,
                        'min1': long['min1'],
                        'max1': long['max1'],
                        'min2': long['min2']
                    }
                else:
                    logger.debug(f"LONG: цена не подходит (low={low}, high={high}, support={support})")

    # --- SHORT ---
    if state['short']['state'] == 'WAIT_ENTRY':
        short = state['short']
        if current_candle['high'] > short['max2'][1]:
            logger.debug("SHORT: структура нарушена (high > max2)")
            state['short']['state'] = 'WAIT_MAX1'
            state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None
        else:
            bars_since = current_idx - short['max2'][0]
            if bars_since < min_bars_after:
                logger.debug(f"SHORT: слишком рано (прошло {bars_since} баров)")
            elif bars_since > max_bars_after:
                logger.debug(f"SHORT: таймаут (прошло {bars_since} баров)")
                state['short']['state'] = 'WAIT_MAX1'
                state['short']['max1'] = state['short']['min1'] = state['short']['max2'] = None
            else:
                t1, t2 = short['max1'][0], short['max2'][0]
                p1, p2 = short['max1'][1], short['max2'][1]
                resistance = p1 + (p2 - p1) * (current_idx - t1) / (t2 - t1)
                low, high = current_candle['low'], current_candle['high']
                if low <= resistance + entry_tolerance and high >= resistance - entry_tolerance:
                    logger.info(f"SHORT: СИГНАЛ ВХОДА на свече {current_idx}, цена {resistance:.2f}")
                    return {
                        'type': 'SHORT',
                        'entry_price': resistance,
                        'entry_idx': current_idx,
                        'max1': short['max1'],
                        'min1': short['min1'],
                        'max2': short['max2']
                    }
                else:
                    logger.debug(f"SHORT: цена не подходит (low={low}, high={high}, resistance={resistance})")
    return None