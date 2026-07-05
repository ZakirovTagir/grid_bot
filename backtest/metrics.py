"""
backtest/metrics.py
Рассчитывает ключевые метрики эффективности стратегии.
"""
import numpy as np
from typing import List, Dict
import pandas as pd

def calculate_metrics(equity_curve: List[float], trades: List[Dict], start_date, end_date):
    if not equity_curve or len(equity_curve) < 2:
        return {
            'cagr': 0.0, 'mdd': 0.0, 'sharpe': 0.0,
            'profit_factor': 0.0, 'win_rate': 0.0,
            'profitable_months_pct': 0.0, 'avg_pnl': 0.0
        }

    equity = np.array(equity_curve)
    days = (end_date - start_date).days
    days = max(days, 1)

    final_balance = equity[-1]
    initial_balance = equity[0]
    cagr = ((final_balance / initial_balance) ** (365 / days) - 1) * 100

    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak * 100
    mdd = np.max(drawdown)

    if len(equity) > 1:
        returns = np.diff(equity) / equity[:-1]
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if np.std(returns) > 0 else 0.0
    else:
        sharpe = 0.0

    if trades:
        pnls = [t['pnl'] for t in trades]
        avg_pnl = np.mean(pnls)
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss != 0 else float('inf')
    else:
        avg_pnl = 0.0
        win_rate = 0.0
        profit_factor = 0.0

    if trades:
        trade_times = [t.get('exit_time', start_date) for t in trades]
        if trade_times:
            months = [t.month for t in trade_times]
            unique_months = set(months)
            profitable_months = sum(1 for m in unique_months
                                    if sum(t['pnl'] for t in trades if t['exit_time'].month == m) > 0)
            profitable_months_pct = profitable_months / len(unique_months) * 100 if unique_months else 0.0
        else:
            profitable_months_pct = 0.0
    else:
        profitable_months_pct = 0.0

    return {
        'cagr': cagr,
        'mdd': mdd,
        'sharpe': sharpe,
        'profit_factor': profit_factor,
        'win_rate': win_rate,
        'profitable_months_pct': profitable_months_pct,
        'avg_pnl': avg_pnl
    }