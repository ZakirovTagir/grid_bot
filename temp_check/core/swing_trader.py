"""
core/swing_trader.py
Классы LongFinder и ShortFinder для поиска свинг-точек в реальном времени.
Взяты из find_swing_points.py.
"""
import pandas as pd
import numpy as np
from typing import Optional, Dict, Tuple
# Вспомогательные функции (копируются из бэктестера)
def is_noisy(row, min_body_ratio=0.3):
    high, low, open_, close = row['high'], row['low'], row['open'], row['close']
    candle_range = high - low
    if candle_range <= 0:
        return True
    body = abs(close - open_)
    return (body / candle_range) < min_body_ratio

def get_block_non_noisy(block):
    non_noisy = []
    for idx, row in block.iterrows():
        if not is_noisy(row):
            non_noisy.append((idx, row))
    return non_noisy

def get_block_extremes(non_noisy):
    if not non_noisy:
        return None, None
    min_row = min(non_noisy, key=lambda x: (x[1]['low'], x[0]))
    max_row = max(non_noisy, key=lambda x: (x[1]['high'], -x[0]))
    return (min_row[1]['low'], min_row[0]), (max_row[1]['high'], max_row[0])

class LongFinder:
    def __init__(self, symbol):
        self.symbol = symbol
        self.state = "WAIT_MIN1"
        self.min1_price = self.min1_idx = None
        self.max1_price = self.max1_idx = None
        self.min2_price = self.min2_idx = None
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
        non_noisy = get_block_non_noisy(block)
        if not non_noisy:
            # print(f"[LONG] Блок {block_start}: все свечи шумные, пропуск.")
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)

        if self.state == "WAIT_MIN1":
            if H_idx > L_idx and (H_price - L_price) >= 300:  # DELTA_PRICE – будем использовать переданное значение
                self.min1_price, self.min1_idx = L_price, L_idx
                self.max1_price, self.max1_idx = H_price, H_idx
                self.state = "WAIT_MIN2"
                # print(f"[LONG] Найдены мин1 и макс1")
            else:
                # print(f"[LONG] Условия не выполнены, сброс.")
                self.reset()
        elif self.state == "WAIT_MIN2":
            if H_price > self.max1_price:
                self.max1_price, self.max1_idx = H_price, H_idx
                # print(f"[LONG] Обновлён макс1")
            if L_price > self.min1_price:
                distance = L_idx - self.min1_idx
                if distance >= 5 and L_idx > self.max1_idx:  # MIN_DISTANCE_BARS = 5
                    # MAX_DELTA_EXTREMES проверка будет снаружи
                    self.min2_price, self.min2_idx = L_price, L_idx
                    self.state = "WAIT_ENTRY"
                    self.pending_trend = {
                        'type': 'LONG',
                        'min1': (self.min1_idx, self.min1_price),
                        'max1': (self.max1_idx, self.max1_price),
                        'min2': (self.min2_idx, self.min2_price)
                    }
                    # print(f"[LONG] Найден мин2 – ожидание входа")
                else:
                    pass  # ждём дальше
            elif L_price < self.min1_price:
                # print(f"[LONG] Перелом вниз, сброс.")
                self.reset()
            else:
                # print(f"[LONG] L == мин1, сброс.")
                self.reset()

    def check_entry(self, current_idx, current_candle):
        if self.state != "WAIT_ENTRY":
            return None
        if current_candle['low'] < self.min2_price:
            # print(f"[LONG] Структура нарушена")
            self.reset()
            return None
        bars_since = current_idx - self.min2_idx
        if bars_since < 3:  # MIN_BARS_AFTER_POINT2
            return None
        if bars_since > 7:  # MAX_BARS_AFTER_POINT2
            # print(f"[LONG] Таймаут входа")
            self.reset()
            return None

        t1, t2 = self.min1_idx, self.min2_idx
        support = self.min1_price + (self.min2_price - self.min1_price) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']

        if low <= support + 50 and high >= support - 50:  # ENTRY_TOLERANCE_USD = 50 (можно параметризовать)
            return {
                'type': 'LONG',
                'entry_price': support,
                'entry_idx': current_idx,
                'min1': (self.min1_idx, self.min1_price),
                'max1': (self.max1_idx, self.max1_price),
                'min2': (self.min2_idx, self.min2_price)
            }
        return None

class ShortFinder:
    def __init__(self, symbol):
        self.symbol = symbol
        self.state = "WAIT_MAX1"
        self.max1_price = self.max1_idx = None
        self.min1_price = self.min1_idx = None
        self.max2_price = self.max2_idx = None
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
        non_noisy = get_block_non_noisy(block)
        if not non_noisy:
            return

        (L_price, L_idx), (H_price, H_idx) = get_block_extremes(non_noisy)

        if self.state == "WAIT_MAX1":
            if L_idx > H_idx and (H_price - L_price) >= 300:
                self.max1_price, self.max1_idx = H_price, H_idx
                self.min1_price, self.min1_idx = L_price, L_idx
                self.state = "WAIT_MAX2"
                # print(f"[SHORT] Найдены макс1 и мин1")
            else:
                self.reset()
        elif self.state == "WAIT_MAX2":
            if L_price < self.min1_price:
                self.min1_price, self.min1_idx = L_price, L_idx
                # print(f"[SHORT] Обновлён мин1")
            if H_price < self.max1_price:
                distance = H_idx - self.max1_idx
                if distance >= 5 and H_idx > self.min1_idx:
                    # MAX_DELTA_EXTREMES проверка снаружи
                    self.max2_price, self.max2_idx = H_price, H_idx
                    self.state = "WAIT_ENTRY"
                    self.pending_trend = {
                        'type': 'SHORT',
                        'max1': (self.max1_idx, self.max1_price),
                        'min1': (self.min1_idx, self.min1_price),
                        'max2': (self.max2_idx, self.max2_price)
                    }
                    # print(f"[SHORT] Найден макс2 – ожидание входа")
                else:
                    pass
            elif H_price > self.max1_price:
                # print(f"[SHORT] Перелом вверх, сброс.")
                self.reset()
            else:
                # print(f"[SHORT] H == макс1, сброс.")
                self.reset()

    def check_entry(self, current_idx, current_candle):
        if self.state != "WAIT_ENTRY":
            return None
        if current_candle['high'] > self.max2_price:
            # print(f"[SHORT] Структура нарушена")
            self.reset()
            return None
        bars_since = current_idx - self.max2_idx
        if bars_since < 3:
            return None
        if bars_since > 7:
            # print(f"[SHORT] Таймаут входа")
            self.reset()
            return None

        t1, t2 = self.max1_idx, self.max2_idx
        resistance = self.max1_price + (self.max2_price - self.max1_price) * (current_idx - t1) / (t2 - t1)
        low, high = current_candle['low'], current_candle['high']

        if low <= resistance + 50 and high >= resistance - 50:
            return {
                'type': 'SHORT',
                'entry_price': resistance,
                'entry_idx': current_idx,
                'max1': (self.max1_idx, self.max1_price),
                'min1': (self.min1_idx, self.min1_price),
                'max2': (self.max2_idx, self.max2_price)
            }
        return None