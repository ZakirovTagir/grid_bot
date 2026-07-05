"""
analyze_losses.py
Анализ сделок: сбор индикаторов на момент входа для выявления порогов фильтрации.
Запускать после успешного бэктеста (должен существовать файл backtest_results.csv).
"""
import pandas as pd
import numpy as np
from datetime import datetime
import os

# Импортируем функции расчёта индикаторов из стратегии (для единообразия)
from core.bb_donchian_strategy import calculate_bollinger_bands, donchian_channel
# Для ATR, ADX, RSI используем библиотеку ta (должна быть установлена)
from ta.volatility import AverageTrueRange
from ta.trend import ADXIndicator
from ta.momentum import RSIIndicator

# Пути
RESULTS_PATH = "backtest_results.csv"
OHLCV_PATH = "data/historical/BTCUSDT_240.csv"

def add_indicators(df: pd.DataFrame, bb_period=10, bb_std=1.0, dc_period=20, atr_period=14):
    """Добавляет все необходимые индикаторы в DataFrame."""
    df = df.copy()
    
    # Полосы Боллинджера
    upper_bb, lower_bb = calculate_bollinger_bands(df, bb_period, bb_std)
    df['upper_bb'] = upper_bb
    df['lower_bb'] = lower_bb
    df['bb_width'] = (upper_bb - lower_bb) / df['close'] * 100
    
    # Канал Дончиана
    upper_dc, lower_dc = donchian_channel(df, dc_period)
    df['upper_dc'] = upper_dc
    df['lower_dc'] = lower_dc
    df['dc_width'] = (upper_dc - lower_dc) / df['close'] * 100
    
    # ATR
    atr_indicator = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=atr_period)
    df['atr'] = atr_indicator.average_true_range()
    df['atr_pct'] = (df['atr'] / df['close']) * 100
    
    # Скользящая средняя ATR (для спайка)
    df['atr_ma'] = df['atr'].rolling(window=atr_period).mean()
    df['atr_spike'] = df['atr'] / df['atr_ma']
    
    # ADX
    adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=atr_period)
    df['adx'] = adx_indicator.adx()
    
    # RSI
    rsi_indicator = RSIIndicator(close=df['close'], window=atr_period)
    df['rsi'] = rsi_indicator.rsi()
    
    return df

def load_trades():
    """Загружает сделки из CSV."""
    if not os.path.exists(RESULTS_PATH):
        raise FileNotFoundError(f"Файл {RESULTS_PATH} не найден. Сначала запустите бэктест с сохранением.")
    df = pd.read_csv(RESULTS_PATH)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['exit_time'] = pd.to_datetime(df['exit_time'])
    return df

def load_ohlcv():
    """Загружает исторические OHLC данные."""
    if not os.path.exists(OHLCV_PATH):
        raise FileNotFoundError(f"Файл {OHLCV_PATH} не найден.")
    df = pd.read_csv(OHLCV_PATH)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    return df

def get_indicators_at_entry(ohlcv: pd.DataFrame, entry_time: datetime, lookback=100) -> dict:
    """
    Вычисляет индикаторы на момент входа (по свече, чей timestamp == entry_time).
    Возвращает словарь со значениями индикаторов.
    """
    # Находим индекс свечи с точно таким же временем
    if entry_time not in ohlcv.index:
        # Если нет точного совпадения, ищем ближайшую предыдущую свечу (не должно быть)
        # Но в данных CSV время – начало периода, а entry_time – конец периода (время закрытия),
        # поэтому в бэктестере entry_time соответствует времени закрытия, а в данных – начало.
        # Нужно найти свечу, у которой timestamp + 4h == entry_time? Упростим: возьмём свечу с timestamp <= entry_time и самую близкую.
        # Но для точности лучше сопоставлять по позиции. Поскольку данные идут строго по 4 часа, можно просто перебирать.
        # Реализуем поиск ближайшей свечи с timestamp <= entry_time.
        idx = ohlcv.index[ohlcv.index <= entry_time][-1]  # последняя свеча до или равная entry_time
    else:
        idx = entry_time
    
    # Убедимся, что есть достаточно данных для индикаторов (хотя бы 50 свечей)
    pos = ohlcv.index.get_loc(idx)
    if pos < 50:
        # Недостаточно данных для стабильных индикаторов, возвращаем NaN
        return {k: np.nan for k in ['atr_pct', 'atr_spike', 'bb_width', 'dc_width', 'adx', 'rsi']}
    
    # Берём окно данных до текущей свечи (включительно)
    window = ohlcv.iloc[:pos+1].copy()
    # Добавляем индикаторы
    window = add_indicators(window)
    # Берём последнюю строку (значения на момент входа)
    row = window.iloc[-1]
    
    # Расстояние до границы Боллинджера
    if 'side' in locals():
        # Здесь side не передаётся, поэтому позже добавим в цикле отдельно
        pass
    
    return {
        'atr_pct': row['atr_pct'],
        'atr_spike': row['atr_spike'],
        'bb_width': row['bb_width'],
        'dc_width': row['dc_width'],
        'adx': row['adx'],
        'rsi': row['rsi']
    }

def analyze():
    """Основная функция анализа."""
    print("Загрузка сделок...")
    trades = load_trades()
    print(f"Загружено {len(trades)} сделок")
    
    print("Загрузка исторических данных...")
    ohlcv = load_ohlcv()
    
    # Собираем данные по сделкам
    rows = []
    for idx, trade in trades.iterrows():
        entry_time = trade['entry_time']
        side = trade['side']
        pnl = trade['pnl']
        
        # Получаем индикаторы на момент входа
        indicators = get_indicators_at_entry(ohlcv, entry_time)
        
        # Дополнительно рассчитаем расстояние до границы Боллинджера (поскольку зависит от стороны)
        # Для этого нужно пересчитать индикаторы для конкретной свечи и стороны
        if entry_time in ohlcv.index:
            idx_ts = entry_time
        else:
            idx_ts = ohlcv.index[ohlcv.index <= entry_time][-1]
        pos = ohlcv.index.get_loc(idx_ts)
        if pos >= 50:
            window = ohlcv.iloc[:pos+1].copy()
            window = add_indicators(window)
            row = window.iloc[-1]
            entry_price = trade['entry_price']
            if side == 'long':
                distance = (entry_price - row['lower_bb']) / entry_price * 100
            else:
                distance = (row['upper_bb'] - entry_price) / entry_price * 100
            indicators['distance_to_bb'] = distance
        else:
            indicators['distance_to_bb'] = np.nan
        
        rows.append({
            'entry_time': entry_time,
            'side': side,
            'pnl': pnl,
            **indicators
        })
    
    df_analysis = pd.DataFrame(rows)
    # Сохраняем полный результат
    df_analysis.to_csv('analysis_results.csv', index=False)
    print("Сохранён файл analysis_results.csv")
    
    # Разделяем на прибыльные и убыточные
    profitable = df_analysis[df_analysis['pnl'] > 0]
    loss = df_analysis[df_analysis['pnl'] <= 0]
    
    print("\n" + "="*60)
    print("Статистика индикаторов на момент входа")
    print("="*60)
    
    # Функция для вывода статистики
    def print_stats(name, df):
        print(f"\n--- {name} (количество: {len(df)}) ---")
        if len(df) == 0:
            print("Нет данных")
            return
        for col in ['atr_pct', 'atr_spike', 'bb_width', 'dc_width', 'adx', 'rsi', 'distance_to_bb']:
            if col in df.columns:
                s = df[col].dropna()
                if len(s) == 0:
                    continue
                print(f"{col:15} | mean={s.mean():.2f} | median={s.median():.2f} | min={s.min():.2f} | max={s.max():.2f} | 25%={s.quantile(0.25):.2f} | 75%={s.quantile(0.75):.2f}")
    
    print_stats("ПРИБЫЛЬНЫЕ СДЕЛКИ", profitable)
    print_stats("УБЫТОЧНЫЕ СДЕЛКИ", loss)
    
    # Дополнительно: гистограммы для ключевых показателей (можно раскомментировать, если нужно визуализировать)
    # import matplotlib.pyplot as plt
    # for col in ['atr_spike', 'bb_width', 'adx']:
    #     plt.hist(profitable[col].dropna(), alpha=0.5, label='profit', bins=20)
    #     plt.hist(loss[col].dropna(), alpha=0.5, label='loss', bins=20)
    #     plt.legend()
    #     plt.title(col)
    #     plt.show()
    
    print("\nРекомендация: посмотрите на значения ATR_spike, bb_width, adx.")
    print("Если у убыточных сделок какой-то индикатор систематически выше/ниже, можно ввести фильтр.")

if __name__ == "__main__":
    analyze()