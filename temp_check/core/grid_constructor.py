"""
core/grid_constructor.py
Построение геометрической (процентной) сетки для нейтральной стратегии.
"""
import math
import numpy as np
from typing import Dict, Tuple, Optional
from config.settings import CAPITAL_RESERVE, GRID_LEVELS_MIN
from config.pairs import PAIRS_CONFIG

def calculate_grid(
    symbol: str,
    upper_bound: float,
    lower_bound: float,
    current_price: float,
    allocated_capital: float
) -> Optional[Dict[str, list]]:
    """
    Возвращает сетку ордеров или None, если невозможно построить.
    Формат результата:
    {
        'buy': [{'price': p, 'qty': q, 'cost_usdt': c}, ...],
        'sell': [{'price': p, 'qty': q, 'cost_usdt': c}, ...]
    }
    """
    if allocated_capital <= 0 or upper_bound <= lower_bound:
        return None

    pair_cfg = PAIRS_CONFIG.get(symbol)
    if not pair_cfg:
        return None
    min_qty = pair_cfg['min_qty']
    price_step = pair_cfg['price_step']
    min_order_usdt = pair_cfg['min_order_usdt']

    # Вычисляем процентный множитель для геометрической прогрессии
    # Количество уровней рассчитываем исходя из капитала и минимальной стоимости ордера
    capital_for_orders = allocated_capital * CAPITAL_RESERVE

    # Предварительная оценка максимального количества уровней
    # При нейтральной стратегии половина капитала идёт на покупки, половина на продажи
    max_levels = int(capital_for_orders / (2 * min_order_usdt))
    if max_levels < GRID_LEVELS_MIN:
        return None

    # Число уровней в каждую сторону (с запасом, потом ограничим по цене)
    N = min(max_levels, 50)  # ограничение сверху, чтобы не перегружать сетку
    if N < GRID_LEVELS_MIN:
        return None

    # Геометрический шаг (процент)
    ratio = (upper_bound / lower_bound) ** (1 / (N - 1))  # множитель между уровнями
    step_pct = (ratio - 1) * 100

    # Генерируем все уровни от нижней границы до верхней
    levels = [lower_bound * (ratio ** i) for i in range(N)]
    # Округляем до шага цены
    levels = [round(lvl / price_step) * price_step for lvl in levels]

    # Текущая цена для определения, где покупка, где продажа
    # Все уровни ниже текущей цены — buy, выше — sell
    buy_levels = []
    sell_levels = []
    for lvl in levels:
        if lvl < current_price * (1 - 0.001):  # допуск 0.1% ниже
            buy_levels.append(lvl)
        elif lvl > current_price * (1 + 0.001):  # 0.1% выше
            sell_levels.append(lvl)
        # Уровень, совпадающий с текущей ценой, пропускаем, чтобы избежать мгновенного исполнения

    # Для каждой стороны ограничиваем количество уровней до N//2 (чтобы соблюсти баланс капитала)
    num_buy = min(len(buy_levels), N // 2)
    num_sell = min(len(sell_levels), N // 2)

    # Отбираем самые близкие уровни к текущей цене (отсекаем дальние)
    buy_levels = sorted(buy_levels, reverse=True)[:num_buy] if buy_levels else []
    sell_levels = sorted(sell_levels)[:num_sell] if sell_levels else []

    # Стоимость одного ордера (равномерно распределяем капитал)
    # Общая сумма для ордеров = капитал, разделенная на покупки и продажи
    total_buy_capital = allocated_capital * CAPITAL_RESERVE * 0.5
    total_sell_capital = allocated_capital * CAPITAL_RESERVE * 0.5

    order_cost = min(
        total_buy_capital / max(len(buy_levels), 1),
        total_sell_capital / max(len(sell_levels), 1)
    )
    if order_cost < min_order_usdt:
        order_cost = min_order_usdt  # минимально допустимый размер

    # Формируем ордера
    buy_orders = []
    for price in buy_levels:
        qty = order_cost / price
        qty = max(min_qty, round(qty / min_qty) * min_qty)  # округление до min_qty
        actual_cost = qty * price
        buy_orders.append({
            'price': price,
            'qty': qty,
            'cost_usdt': actual_cost
        })

    sell_orders = []
    for price in sell_levels:
        qty = order_cost / price
        qty = max(min_qty, round(qty / min_qty) * min_qty)
        actual_cost = qty * price
        sell_orders.append({
            'price': price,
            'qty': qty,
            'cost_usdt': actual_cost
        })

    # Финальная проверка минимального количества уровней
    if len(buy_orders) < GRID_LEVELS_MIN or len(sell_orders) < GRID_LEVELS_MIN:
        return None

    return {'buy': buy_orders, 'sell': sell_orders}