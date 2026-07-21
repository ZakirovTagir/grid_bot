"""
core/swing_trader.py
Классы LongFinder и ShortFinder для поиска свинг-точек в реальном времени.
Параметры стратегии передаются через конструктор (или методы) из конфигурации пары.
"""
import pandas as pd
import logging
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# Вспомогательные функции (без изменений)
def is_noisy(row, min_body_ratio=0.3):
    high, low, open_, close = row['high'], row['low'], row['open'], row['close']
    candle_range = high - low
    if candle_range <= 0:
        return True
    body = abs(close - open_)
    return (body / candle_range) < min_body_ratio

def get_block_non_noisy(block, min_body_ratio=0.3):
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

class LongFinder:
    def __init__(self, symbol, params: Optional[Dict] = None):
        self.symbol = symbol
        self.params = params or {}
        self.state = "WAIT_MIN1"
        self.min1_price = self.min1_idx = None
        self.max1_price = self.max1_idx = None
        self.min2_price = self.min2_idx = None
        self.pending_trend = None

    def reset(self):
        logger.info(f"[{self.symbol}] LONG сброс состояния из {self.state}")
        self.state = "WAIT_MIN1"
        self.min1_price = self.min1_idx = None
        self.max1_price = self.max1_idx = None
        self.min2_price = self.min2_idx = None
        self.pending_trend = None

    def process_block(self, block_start, block):
        if self.state == "WAIT_ENTRY":
            logger.info(f"[{self.symbol}] LONG WAIT_ENTRY, блок {block_start} пропущен")
            return

        min_body_ratio = self.params.get('MIN_BODY_RATIO', 0.3)
        non_noisy = get_block_non_noisy(block, min_body_ratio)
        if not non_noisy:
            logger.info(f"[{self.symbol}] LONG блок {block_start}: все свечи шумные, пропуск")
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)
        delta_price = self.params.get('DELTA_PRICE', 300)
        min_distance = self.params.get('MIN_DISTANCE_BARS', 5)
        max_delta = self.params.get('MAX_DELTA_EXTREMES', 2500)

        logger.info(f"[{self.symbol}] LONG блок {block_start}-{block_start+6}: L={L_price:.2f} (idx {L_idx}), H={H_price:.2f} (idx {H_idx}), состояние={self.state}")

        if self.state == "WAIT_MIN1":
            if H_idx > L_idx and (H_price - L_price) >= delta_price:
                self.min1_price, self.min1_idx = L_price, L_idx
                self.max1_price, self.max1_idx = H_price, H_idx
                self.state = "WAIT_MIN2"
                logger.info(f"[{self.symbol}] LONG найдены мин1={self.min1_price:.2f} (idx {self.min1_idx}), макс1={self.max1_price:.2f} (idx {self.max1_idx})")
            else:
                logger.info(f"[{self.symbol}] LONG блок {block_start}: условия WAIT_MIN1 не выполнены (delta={H_price-L_price:.2f}, need>={delta_price})")
                self.reset()
        elif self.state == "WAIT_MIN2":
            if H_price > self.max1_price:
                self.max1_price, self.max1_idx = H_price, H_idx
                logger.info(f"[{self.symbol}] LONG обновлён макс1 до {self.max1_price:.2f} (idx {self.max1_idx})")
            if L_price > self.min1_price:
                distance = L_idx - self.min1_idx
                if distance >= min_distance and L_idx > self.max1_idx:
                    if max_delta > 0 and (L_price - self.min1_price) > max_delta:
                        logger.info(f"[{self.symbol}] LONG разница мин2-мин1 > {max_delta}, сброс")
                        self.reset()
                        return
                    self.min2_price, self.min2_idx = L_price, L_idx
                    self.state = "WAIT_ENTRY"
                    self.pending_trend = {
                        'type': 'LONG',
                        'min1': (self.min1_idx, self.min1_price),
                        'max1': (self.max1_idx, self.max1_price),
                        'min2': (self.min2_idx, self.min2_price)
                    }
                    logger.info(f"[{self.symbol}] LONG найден мин2={self.min2_price:.2f} (idx {self.min2_idx}) – переход в WAIT_ENTRY")
                else:
                    logger.info(f"[{self.symbol}] LONG кандидат в мин2 не подходит (дистанция {distance}, мин. {min_distance})")
            elif L_price < self.min1_price:
                logger.info(f"[{self.symbol}] LONG перелом вниз, сброс")
                self.reset()
            else:
                logger.info(f"[{self.symbol}] LONG L == мин1, сброс")
                self.reset()

    def check_entry(self, current_idx, current_candle):
        logger.info(f"[{self.symbol}] LONG check_entry: idx={current_idx}, state={self.state}")
        if self.state != "WAIT_ENTRY":
            return None

        min_bars_after = self.params.get('MIN_BARS_AFTER_POINT2', 3)
        max_bars_after = self.params.get('MAX_BARS_AFTER_POINT2', 7)
        entry_tolerance = self.params.get('ENTRY_TOLERANCE_USD', 50)

        logger.info(f"[{self.symbol}] LONG проверка входа: idx={current_idx}")
        if current_candle['low'] < self.min2_price:
            logger.info(f"[{self.symbol}] LONG структура нарушена (low={current_candle['low']} < min2={self.min2_price})")
            self.reset()
            return None
        bars_since = current_idx - self.min2_idx
        if bars_since < min_bars_after:
            logger.info(f"[{self.symbol}] LONG слишком рано для входа (прошло {bars_since} баров, нужно {min_bars_after})")
            return None
        if bars_since > max_bars_after:
            logger.info(f"[{self.symbol}] LONG таймаут входа (прошло {bars_since} баров, максимум {max_bars_after})")
            self.reset()
            return None

        t1, t2 = self.min1_idx, self.min2_idx
        support = self.min1_price + (self.min2_price - self.min1_price) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']

        if low <= support + entry_tolerance and high >= support - entry_tolerance:
            logger.info(f"[{self.symbol}] LONG СИГНАЛ ВХОДА: цена {support:.2f}, low={low:.2f}, high={high:.2f}, tolerance={entry_tolerance}")
            return {
                'type': 'LONG',
                'entry_price': support,
                'entry_idx': current_idx,
                'min1': (self.min1_idx, self.min1_price),
                'max1': (self.max1_idx, self.max1_price),
                'min2': (self.min2_idx, self.min2_price)
            }
        else:
            logger.info(f"[{self.symbol}] LONG вход не сработал: low={low:.2f}, high={high:.2f}, support={support:.2f}, tolerance={entry_tolerance}")
            return None

class ShortFinder:
    def __init__(self, symbol, params: Optional[Dict] = None):
        self.symbol = symbol
        self.params = params or {}
        self.state = "WAIT_MAX1"
        self.max1_price = self.max1_idx = None
        self.min1_price = self.min1_idx = None
        self.max2_price = self.max2_idx = None
        self.pending_trend = None

    def reset(self):
        logger.info(f"[{self.symbol}] SHORT сброс состояния из {self.state}")
        self.state = "WAIT_MAX1"
        self.max1_price = self.max1_idx = None
        self.min1_price = self.min1_idx = None
        self.max2_price = self.max2_idx = None
        self.pending_trend = None

    def process_block(self, block_start, block):
        if self.state == "WAIT_ENTRY":
            logger.info(f"[{self.symbol}] SHORT WAIT_ENTRY, блок {block_start} пропущен")
            return

        min_body_ratio = self.params.get('MIN_BODY_RATIO', 0.3)
        non_noisy = get_block_non_noisy(block, min_body_ratio)
        if not non_noisy:
            logger.info(f"[{self.symbol}] SHORT блок {block_start}: все свечи шумные, пропуск")
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)
        delta_price = self.params.get('DELTA_PRICE', 300)
        min_distance = self.params.get('MIN_DISTANCE_BARS', 5)
        max_delta = self.params.get('MAX_DELTA_EXTREMES', 2500)

        logger.info(f"[{self.symbol}] SHORT блок {block_start}-{block_start+6}: L={L_price:.2f} (idx {L_idx}), H={H_price:.2f} (idx {H_idx}), состояние={self.state}")

        if self.state == "WAIT_MAX1":
            if L_idx > H_idx and (H_price - L_price) >= delta_price:
                self.max1_price, self.max1_idx = H_price, H_idx
                self.min1_price, self.min1_idx = L_price, L_idx
                self.state = "WAIT_MAX2"
                logger.info(f"[{self.symbol}] SHORT найдены макс1={self.max1_price:.2f} (idx {self.max1_idx}), мин1={self.min1_price:.2f} (idx {self.min1_idx})")
            else:
                logger.info(f"[{self.symbol}] SHORT блок {block_start}: условия WAIT_MAX1 не выполнены (delta={H_price-L_price:.2f}, need>={delta_price})")
                self.reset()
        elif self.state == "WAIT_MAX2":
            if L_price < self.min1_price:
                self.min1_price, self.min1_idx = L_price, L_idx
                logger.info(f"[{self.symbol}] SHORT обновлён мин1 до {self.min1_price:.2f} (idx {self.min1_idx})")
            if H_price < self.max1_price:
                distance = H_idx - self.max1_idx
                if distance >= min_distance and H_idx > self.min1_idx:
                    if max_delta > 0 and (self.max1_price - H_price) > max_delta:
                        logger.info(f"[{self.symbol}] SHORT разница макс1-макс2 > {max_delta}, сброс")
                        self.reset()
                        return
                    self.max2_price, self.max2_idx = H_price, H_idx
                    self.state = "WAIT_ENTRY"
                    self.pending_trend = {
                        'type': 'SHORT',
                        'max1': (self.max1_idx, self.max1_price),
                        'min1': (self.min1_idx, self.min1_price),
                        'max2': (self.max2_idx, self.max2_price)
                    }
                    logger.info(f"[{self.symbol}] SHORT найден макс2={self.max2_price:.2f} (idx {self.max2_idx}) – переход в WAIT_ENTRY")
                else:
                    logger.info(f"[{self.symbol}] SHORT кандидат в макс2 не подходит (дистанция {distance}, мин. {min_distance})")
            elif H_price > self.max1_price:
                logger.info(f"[{self.symbol}] SHORT перелом вверх, сброс")
                self.reset()
            else:
                logger.info(f"[{self.symbol}] SHORT H == макс1, сброс")
                self.reset()

    def check_entry(self, current_idx, current_candle):
        logger.info(f"[{self.symbol}] SHORT check_entry: idx={current_idx}, state={self.state}")
        if self.state != "WAIT_ENTRY":
            return None

        min_bars_after = self.params.get('MIN_BARS_AFTER_POINT2', 3)
        max_bars_after = self.params.get('MAX_BARS_AFTER_POINT2', 7)
        entry_tolerance = self.params.get('ENTRY_TOLERANCE_USD', 50)

        logger.info(f"[{self.symbol}] SHORT проверка входа: idx={current_idx}")
        if current_candle['high'] > self.max2_price:
            logger.info(f"[{self.symbol}] SHORT структура нарушена (high={current_candle['high']} > max2={self.max2_price})")
            self.reset()
            return None
        bars_since = current_idx - self.max2_idx
        if bars_since < min_bars_after:
            logger.info(f"[{self.symbol}] SHORT слишком рано для входа (прошло {bars_since} баров, нужно {min_bars_after})")
            return None
        if bars_since > max_bars_after:
            logger.info(f"[{self.symbol}] SHORT таймаут входа (прошло {bars_since} баров, максимум {max_bars_after})")
            self.reset()
            return None

        t1, t2 = self.max1_idx, self.max2_idx
        resistance = self.max1_price + (self.max2_price - self.max1_price) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']

        if low <= resistance + entry_tolerance and high >= resistance - entry_tolerance:
            logger.info(f"[{self.symbol}] SHORT СИГНАЛ ВХОДА: цена {resistance:.2f}, low={low:.2f}, high={high:.2f}, tolerance={entry_tolerance}")
            return {
                'type': 'SHORT',
                'entry_price': resistance,
                'entry_idx': current_idx,
                'max1': (self.max1_idx, self.max1_price),
                'min1': (self.min1_idx, self.min1_price),
                'max2': (self.max2_idx, self.max2_price)
            }
        else:
            logger.info(f"[{self.symbol}] SHORT вход не сработал: low={low:.2f}, high={high:.2f}, resistance={resistance:.2f}, tolerance={entry_tolerance}")
            return None