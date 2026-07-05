"""
core/market_analyzer.py
Функции определения фазы рынка (флет / ралли / затухание).
Использует библиотеку ta для индикаторов.
"""
import pandas as pd
import numpy as np
from ta.trend import ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from config.settings import (
    ADX_TREND_THRESHOLD, ADX_FLAT_THRESHOLD, BB_WIDTH_MIN_PCT,
    AMPLITUDE_CV_MAX, TOUCHES_MIN, EMA_CHANGE_MAX,
    ATR_DECAY_THRESHOLD, ATR_PERIOD, ATR_CANDLES_DECAY
)

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет в DataFrame колонки с индикаторами."""
    # ADX
    adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
    df['adx'] = adx_indicator.adx()

    # Полосы Боллинджера (20, 2)
    bb = BollingerBands(close=df['close'], window=20, window_dev=2)
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_lower'] = bb.bollinger_lband()
    df['bb_width_pct'] = (df['bb_upper'] - df['bb_lower']) / bb.bollinger_mavg() * 100

    # ATR и ATR%
    atr_obj = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=ATR_PERIOD)
    df['atr'] = atr_obj.average_true_range()
    df['atr_pct'] = df['atr'] / df['close'] * 100

    # EMA(20) для фильтра тренда
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()

    return df

def is_rally(df: pd.DataFrame) -> bool:
    """Пара в ралли, если ADX > 25 или сильный наклон EMA."""
    required_cols = ['adx', 'ema20']
    if not all(col in df.columns for col in required_cols):
        df = add_indicators(df)

    # Основное условие: ADX > 25
    if df['adx'].iloc[-1] > ADX_TREND_THRESHOLD:
        return True

    # Дополнительное: сильный наклон EMA за последние 24 часа (6 баров на 4H)
    if len(df) >= 6:
        ema_change = (df['ema20'].iloc[-1] - df['ema20'].iloc[-6]) / df['ema20'].iloc[-6] * 100
        if abs(ema_change) > EMA_CHANGE_MAX:
            return True

    return False

def is_flat(df: pd.DataFrame) -> bool:
    """Проверяет наличие боковика с достаточной амплитудой."""
    if 'bb_width_pct' not in df.columns:
        df = add_indicators(df)

    # 1. ADX < 20 (отсутствие тренда)
    if df['adx'].iloc[-1] >= ADX_FLAT_THRESHOLD:
        return False

    # 2. Ширина полос Боллинджера > 3%
    if df['bb_width_pct'].iloc[-1] < BB_WIDTH_MIN_PCT:
        return False

    # 3. Стабильность амплитуды (High-Low) за 48 часов
    # Берём последние 12 баров 4H (48 часов)
    window = df.tail(12) if len(df) >= 12 else df
    amplitude = window['high'] - window['low']
    cv = amplitude.std() / amplitude.mean() * 100 if amplitude.mean() != 0 else 100
    if cv > AMPLITUDE_CV_MAX:
        return False

    # 4. Касания границ (верхней/нижней) за 48 часов
    upper_bound = window['high'].max()
    lower_bound = window['low'].min()
    touches = 0
    for i in range(len(window)):
        idx = window.index[i]
        if window['high'].loc[idx] >= upper_bound * 0.998 or window['low'].loc[idx] <= lower_bound * 1.002:
            touches += 1
    if touches < TOUCHES_MIN:
        return False

    return True

def is_volatility_decay(df: pd.DataFrame) -> bool:
    """Затухание: ATR% < порога две свечи подряд."""
    if 'atr_pct' not in df.columns:
        df = add_indicators(df)

    if len(df) < ATR_CANDLES_DECAY:
        return False
    last_two = df['atr_pct'].iloc[-ATR_CANDLES_DECAY:]
    return (last_two < ATR_DECAY_THRESHOLD).all() and last_two.notna().all()