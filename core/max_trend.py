"""
core/max_trend.py
Простой вариант MaxTrend: пробой High/Low в направлении тренда.
"""
import pandas as pd
import logging
from typing import Dict, Optional
from ta.trend import EMAIndicator

logger = logging.getLogger('max_trend')


def add_ema(df: pd.DataFrame, period: int) -> pd.Series:
    ema = EMAIndicator(close=df['close'], window=period)
    return ema.ema_indicator()


def max_trend_strict_signal(df: pd.DataFrame, params: Dict,
                            position: Optional[Dict] = None) -> str:
    ema_slow = params.get('ema_slow', 200)
    rsi_period = params.get('rsi_period', 14)

    if len(df) < ema_slow + 5:
        return ''

    from ta.momentum import RSIIndicator
    ema_slow_vals = add_ema(df, ema_slow)
    rsi = RSIIndicator(close=df['close'], window=rsi_period).rsi().iloc[-1]
    last_close = df['close'].iloc[-1]
    last_low = df['low'].iloc[-1]
    last_high = df['high'].iloc[-1]

    # Тренд
    if last_close > ema_slow_vals.iloc[-1]:
        trend = 'bull'
    elif last_close < ema_slow_vals.iloc[-1]:
        trend = 'bear'
    else:
        return ''

    # Выход (трейлинг 4 свечи)
    if position:
        side = position.get('side')
        if len(df) >= 4:
            if side == 'long' and last_low <= df['low'].iloc[-5:-1].min():
                return 'CLOSE_LONG'
            if side == 'short' and last_high >= df['high'].iloc[-5:-1].max():
                return 'CLOSE_SHORT'

    # Вход: откат к EMA200 + RSI
    if trend == 'bull' and last_low <= ema_slow_vals.iloc[-1] and rsi < 45:
        return 'BUY'
    elif trend == 'bear' and last_high >= ema_slow_vals.iloc[-1] and rsi > 55:
        return 'SELL'

    return ''

    # --- Выход из позиции (простой трейлинг по минимуму/максимуму 4 свечей) ---
    if position:
        side = position.get('side')
        if len(df) >= 4:
            if side == 'long' and last_low <= df['low'].iloc[-5:-1].min():
                return 'CLOSE_LONG'
            if side == 'short' and last_high >= df['high'].iloc[-5:-1].max():
                return 'CLOSE_SHORT'

    # --- Вход на пробое High/Low ---
    if len(df) < 5:
        return ''

    prev_high = df['high'].iloc[-5:-1].max()
    prev_low = df['low'].iloc[-5:-1].min()

    if trend == 'bull' and last_high > prev_high:
        return 'BUY'
    elif trend == 'bear' and last_low < prev_low:
        return 'SELL'

    return ''


def max_trend_signal(df: pd.DataFrame, params: Dict) -> str:
    return max_trend_strict_signal(df, params)