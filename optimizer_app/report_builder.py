"""
optimizer_app/report_builder.py
Строит текстовый аналитический отчёт для отправки в DeepSeek.
"""
import numpy as np
from typing import Dict, List
from datetime import datetime

def build_report(
    symbol: str,
    period_text: str,
    metrics: Dict,
    current_params: Dict,
    top_alternatives: List[Dict]
) -> str:
    """
    Формирует отчёт в свободной текстовой форме.
    metrics: словарь с CAGR, MDD, Sharpe, profitable_months_pct, avg_pnl, win_rate
    current_params: текущие параметры стратегии
    top_alternatives: список лучших альтернатив с ключами 'params', 'metrics', 'strategy'
    """
    report = f"""
АНАЛИТИЧЕСКИЙ ОТЧЁТ ПО ТОРГОВОЙ ПАРЕ {symbol}
Дата формирования: {datetime.now().strftime('%Y-%m-%d %H:%M')}
Рассматриваемый период: {period_text}

--- ТЕКУЩАЯ СТРАТЕГИЯ ---
Параметры: {current_params}

Метрики:
- Годовая доходность (CAGR): {metrics.get('cagr', 0):.2f}%
- Максимальная просадка (MDD): {metrics.get('mdd', 0):.2f}%
- Коэффициент Шарпа: {metrics.get('sharpe', 0):.2f}
- Процент прибыльных месяцев: {metrics.get('profitable_months_pct', 0):.2f}%
- Средний PnL на сделку: {metrics.get('avg_pnl', 0):.4f} USDT
- Процент прибыльных сделок: {metrics.get('win_rate', 0):.2f}%

--- ТОП-3 АЛЬТЕРНАТИВНЫХ ВАРИАНТОВ ---
"""
    for i, alt in enumerate(top_alternatives, 1):
        report += f"\nВариант {i} (стратегия: {alt.get('strategy', 'Grid')}):\n"
        report += f"  Параметры: {alt['params']}\n"
        report += f"  Метрики: CAGR={alt['metrics'].get('cagr', 0):.2f}%, "
        report += f"MDD={alt['metrics'].get('mdd', 0):.2f}%, "
        report += f"Шарп={alt['metrics'].get('sharpe', 0):.2f}\n"

    report += "\n--- ЗАПРОС ---\n"
    report += ("На основе этих данных предложи оптимальные параметры для торговли. "
               "Объясни выбор и укажи потенциальные риски.")

    return report