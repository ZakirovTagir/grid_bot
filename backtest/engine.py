"""
backtest/engine.py
Модуль бэктестинга на исторических данных.
Поддерживает Grid, MaxTrend и BollDonchian (без кулдауна/серий убытков).
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional
import logging

from core.market_analyzer import is_rally, is_flat, is_volatility_decay, add_indicators
from core.grid_constructor import calculate_grid
from core.max_trend import max_trend_strict_signal
from core.bb_donchian_strategy import bb_donchian_signal
from core.risk_manager import GlobalRiskMonitor, PositionTracker
from config.settings import (
    CAPITAL_RESERVE, TAKER_FEE, SLIPPAGE, TRAILING_STOP_PCT,
    COOLDOWN_HOURS, MAX_POSITIONS, CAPITAL_PER_POSITION_PCT
)

logger = logging.getLogger(__name__)


class BacktestEngine:
    def __init__(self, symbols: List[str], start_capital: float, start_date: datetime, end_date: datetime):
        self.symbols = symbols
        self.start_capital = start_capital
        self.start_date = start_date
        self.end_date = end_date
        self.data: Dict[str, pd.DataFrame] = {}
        self.results = []

    def load_data(self):
        for sym in self.symbols:
            path = f"data/historical/{sym}_240.csv"
            if os.path.exists(path):
                df = pd.read_csv(path)
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                df = df[(df['timestamp'] >= self.start_date) & (df['timestamp'] <= self.end_date)]
                if not df.empty:
                    self.data[sym] = df
                    logger.info(f"Загружено {len(df)} свечей для {sym}")
                else:
                    logger.warning(f"Нет данных в указанном диапазоне для {sym}")
            else:
                logger.warning(f"Файл не найден: {path}")

    def run(self, strategy_func=None, strategy_name='Grid', params: Optional[Dict] = None, quiet=False):
        self.load_data()
        if not self.data:
            logger.error("Нет данных для симуляции")
            return

        all_timestamps = sorted(set(ts for df in self.data.values() for ts in df['timestamp']))
        if not quiet:
            logger.info(f"Всего временных меток: {len(all_timestamps)}")

        balance = self.start_capital
        risk_monitor = GlobalRiskMonitor(initial_balance=balance)
        if strategy_name == 'BollDonchian' and params:
            if 'max_drawdown_global' in params:
                risk_monitor.max_drawdown_global = params['max_drawdown_global']
            if 'max_concurrent_positions' in params:
                risk_monitor.max_positions = params['max_concurrent_positions']
        else:
            risk_monitor.max_drawdown_global = getattr(risk_monitor, 'max_drawdown_global', 20.0)
            risk_monitor.max_positions = MAX_POSITIONS

        risk_monitor.total_risk_percent = 0.0
        open_positions = {}

        for current_time in all_timestamps:
            # === РАСЧЁТ ЭКВИТИ ===
            if strategy_name == 'BollDonchian':
                market_value = 0.0
                for sym, pos in open_positions.items():
                    mask = self.data[sym]['timestamp'] <= current_time
                    if mask.sum() == 0:
                        continue
                    price = self.data[sym].loc[mask, 'close'].iloc[-1]
                    qty = sum(p['qty'] for p in pos['parts'])
                    if pos['side'] == 'long':
                        market_value += qty * price
                    else:  # short
                        market_value -= qty * price
                equity = balance + market_value
            else:
                # Старый расчёт для Grid/MaxTrend
                total_unrealised = 0.0
                for sym, pos in open_positions.items():
                    df_sym = self.data[sym]
                    mask = df_sym['timestamp'] <= current_time
                    if mask.sum() == 0:
                        continue
                    current_price = df_sym.loc[mask, 'close'].iloc[-1]
                    unreal = self._total_unrealised_pnl(pos, current_price)
                    total_unrealised += unreal
                equity = balance + total_unrealised

            risk_monitor.update_balance(equity)

            # Проверка глобальной просадки
            if risk_monitor.is_global_drawdown_exceeded():
                dd_info = risk_monitor.get_drawdown_info()
                logger.warning(
                    f"ГЛОБАЛЬНАЯ ПРОСАДКА: пик={dd_info['peak_balance']:.2f}, "
                    f"текущий баланс={dd_info['current_balance']:.2f}, "
                    f"просадка={dd_info['drawdown_pct']:.1f}%"
                )
                if open_positions:
                    worst_sym = min(open_positions, key=lambda s: self._total_unrealised_pnl(
                        open_positions[s],
                        current_price=self.data[s].loc[self.data[s]['timestamp'] <= current_time, 'close'].iloc[-1]
                    ))
                    balance, removed_risk, pnl = self._close_position(
                        worst_sym, open_positions[worst_sym], balance, current_time,
                        reason='global_drawdown'
                    )
                    risk_monitor.remove_position_risk(removed_risk)
                    del open_positions[worst_sym]
                    risk_monitor.reduce_exposure()
                    # Пересчёт equity после закрытия
                    if strategy_name == 'BollDonchian':
                        market_value = 0.0
                        for sym, pos in open_positions.items():
                            mask = self.data[sym]['timestamp'] <= current_time
                            if mask.sum() == 0:
                                continue
                            price = self.data[sym].loc[mask, 'close'].iloc[-1]
                            qty = sum(p['qty'] for p in pos['parts'])
                            if pos['side'] == 'long':
                                market_value += qty * price
                            else:
                                market_value -= qty * price
                        equity = balance + market_value
                    else:
                        total_unrealised = 0.0
                        for sym, pos in open_positions.items():
                            mask = self.data[sym]['timestamp'] <= current_time
                            if mask.sum() == 0:
                                continue
                            price = self.data[sym].loc[mask, 'close'].iloc[-1]
                            unreal = self._total_unrealised_pnl(pos, price)
                            total_unrealised += unreal
                        equity = balance + total_unrealised
                    risk_monitor.update_balance(equity)

            total_risk_used = sum(pos.get('risk_percent', 0.0) for pos in open_positions.values())
            risk_monitor.total_risk_percent = total_risk_used

            for sym, df in self.data.items():
                mask = df['timestamp'] <= current_time
                if mask.sum() < 14:
                    continue
                if strategy_name == 'MaxTrend':
                    window_data = df[mask].tail(300).copy()
                elif strategy_name == 'BollDonchian':
                    required = max(params.get('donchian_period', 20), params.get('bb_period', 20)) + 5
                    window_data = df[mask].tail(required).copy()
                else:
                    window_data = df[mask].tail(48).copy()
                if len(window_data) < 14:
                    continue

                current_price = window_data['close'].iloc[-1]
                if risk_monitor.is_in_cooldown(sym):
                    continue

                if strategy_name == 'BollDonchian':
                    signal_dict = bb_donchian_signal(
                        df=window_data,
                        params=params if params else {},
                        position=open_positions.get(sym),
                        equity=equity,
                        total_risk_used=risk_monitor.total_risk_percent
                    )
                    action = signal_dict.get('action', 'NONE')

                    if action in ('BUY', 'SELL'):
                        risk_pct = signal_dict.get('risk_percent', 0.0)
                        if len(open_positions) < risk_monitor.max_positions and risk_monitor.can_open_position(risk_pct):
                            side = 'long' if action == 'BUY' else 'short'
                            size_usdt = signal_dict.get('size_usdt', 0.0)
                            stop_loss_price = signal_dict.get('stop_loss_price', 0.0)
                            if size_usdt <= 0 or stop_loss_price <= 0:
                                continue
                            if size_usdt < 10:
                                continue
                            qty = size_usdt / current_price
                            parts = [{
                                'entry_price': current_price,
                                'qty': qty,
                                'risk': risk_pct,
                                'allocated': size_usdt,
                                'entry_time': current_time
                            }]
                            open_positions[sym] = {
                                'side': side,
                                'strategy': strategy_name,
                                'params': params.copy() if params else {},
                                'parts': parts,
                                'avg_price': current_price,
                                'unrealised_pnl': 0.0,
                                'total_risk': risk_pct,
                                'stop_loss': stop_loss_price,
                                'candles_held': 0,
                                'trailing_activated': False,
                                'highest_price': current_price,
                                'lowest_price': current_price,
                                'entry_price': current_price,
                                'risk_percent': risk_pct,
                                'symbol': sym
                            }
                            # === ИСПРАВЛЕНИЕ ЗНАКА ДЛЯ ШОРТА ===
                            if side == 'long':
                                balance -= size_usdt
                            else:  # short — получаем кэш от продажи
                                balance += size_usdt

                            risk_monitor.add_position_risk(risk_pct)
                            if not quiet:
                                logger.info(
                                    f"BollDonchian открыта {side} позиция {sym} в {current_time}, "
                                    f"цена {current_price:.2f}, размер {size_usdt:.2f} USDT, "
                                    f"стоп {stop_loss_price:.2f}, риск {risk_pct:.2f}%"
                                )
                        else:
                            if not quiet:
                                logger.debug(
                                    f"BollDonchian: нельзя открыть {sym}, позиций "
                                    f"{len(open_positions)}/{risk_monitor.max_positions}, "
                                    f"риск {risk_pct:.2f}%"
                                )

                    elif action == 'CLOSE' and sym in open_positions:
                        reason = signal_dict.get('reason', 'exit_signal')
                        exit_price = signal_dict.get('exit_price', None)
                        if not quiet:
                            logger.info(f"BollDonchian закрытие {sym} по причине {reason} в {current_time}")
                        balance, removed_risk, pnl = self._close_position(
                            sym, open_positions[sym], balance, current_time,
                            close_price=exit_price, reason=reason
                        )
                        risk_monitor.remove_position_risk(removed_risk)
                        risk_monitor.register_trade_result(sym, pnl)
                        del open_positions[sym]
                        risk_monitor.set_cooldown(sym)

                    elif sym in open_positions and action == 'NONE' and 'updated_position' in signal_dict:
                        pos = open_positions[sym]
                        upd = signal_dict['updated_position']
                        if 'stop_loss' in upd:
                            pos['stop_loss'] = upd['stop_loss']
                        if 'candles_held' in upd:
                            pos['candles_held'] = upd['candles_held']
                        if 'trailing_activated' in upd:
                            pos['trailing_activated'] = upd['trailing_activated']
                        if 'highest_price' in upd:
                            pos['highest_price'] = upd['highest_price']
                        if 'lowest_price' in upd:
                            pos['lowest_price'] = upd['lowest_price']
                        open_positions[sym] = pos
                    continue

                # ======= Старая логика для Grid и MaxTrend =======
                if sym in open_positions:
                    pos = open_positions[sym]
                    pos['avg_price'] = self._calc_avg_price(pos)
                    total_unrealised = self._total_unrealised_pnl(pos, current_price)
                    pos['unrealised_pnl'] = total_unrealised

                    if pos.get('strategy') == 'MaxTrend':
                        exit_signal = ''
                        if strategy_func:
                            exit_signal = strategy_func(window_data, pos.get('params', {}), position={'side': pos['side'], 'avg_price': pos['avg_price']})
                        if exit_signal in ('CLOSE_LONG', 'CLOSE_SHORT'):
                            if not quiet:
                                logger.info(f"MaxTrend сигнал выхода {sym} в {current_time}: {exit_signal}")
                            balance, removed_risk, pnl = self._close_position(sym, pos, balance, current_time, reason='exit_signal')
                            risk_monitor.remove_position_risk(removed_risk)
                            equity = balance
                            risk_monitor.set_cooldown(sym)
                            del open_positions[sym]
                            continue
                        if exit_signal in ('ADD_LONG', 'ADD_SHORT'):
                            self._try_pyramid_add(sym, pos, current_price, window_data, params, balance, current_time)
                        if self._check_max_trend_stop(sym, pos, window_data, pos.get('params', {})):
                            if not quiet:
                                logger.info(f"MaxTrend трейлинг-стоп {sym} в {current_time}")
                            balance, removed_risk, pnl = self._close_position(sym, pos, balance, current_time, reason='trailing_stop')
                            risk_monitor.remove_position_risk(removed_risk)
                            equity = balance
                            risk_monitor.set_cooldown(sym)
                            del open_positions[sym]
                            continue
                    else:
                        if pos['unrealised_pnl'] > pos.get('high_water_mark', 0):
                            pos['high_water_mark'] = pos['unrealised_pnl']
                        if pos.get('high_water_mark', 0) > 0 and pos['unrealised_pnl'] > 0:
                            drop = (pos['high_water_mark'] - pos['unrealised_pnl']) / pos['high_water_mark']
                            if drop >= TRAILING_STOP_PCT / 100:
                                if not quiet:
                                    logger.info(f"Grid трейлинг-стоп {sym} в {current_time}")
                                balance, removed_risk, pnl = self._close_position(sym, pos, balance, current_time, reason='trailing_stop')
                                risk_monitor.remove_position_risk(removed_risk)
                                equity = balance
                                risk_monitor.set_cooldown(sym)
                                del open_positions[sym]
                                continue
                        if is_volatility_decay(window_data):
                            if not quiet:
                                logger.info(f"Затухание волатильности {sym} в {current_time}")
                            balance, removed_risk, pnl = self._close_position(sym, pos, balance, current_time, reason='volatility_decay')
                            risk_monitor.remove_position_risk(removed_risk)
                            equity = balance
                            risk_monitor.set_cooldown(sym)
                            del open_positions[sym]
                            continue

                elif len(open_positions) < risk_monitor.max_positions:
                    signal = ''
                    if strategy_name == 'MaxTrend':
                        func = strategy_func if strategy_func else max_trend_strict_signal
                        signal = func(window_data, params if params else {}, position=None)
                    else:
                        if not is_rally(window_data) and is_flat(window_data):
                            signal = 'BUY'

                    if signal in ('BUY', 'SELL'):
                        side = 'long' if signal == 'BUY' else 'short'
                        risk_pct = params.get('risk_per_trade', 0.03) if params else 0.03
                        entry_cost = equity * risk_pct / 0.20
                        qty = entry_cost / current_price
                        if entry_cost < 10 or qty <= 0:
                            continue
                        parts = [{
                            'entry_price': current_price,
                            'qty': qty,
                            'risk': risk_pct,
                            'allocated': entry_cost,
                            'entry_time': current_time
                        }]
                        open_positions[sym] = {
                            'side': side,
                            'strategy': strategy_name,
                            'params': params.copy() if params else {},
                            'parts': parts,
                            'avg_price': current_price,
                            'unrealised_pnl': 0.0,
                            'total_risk': risk_pct,
                            'risk_percent': risk_pct * 100
                        }
                        balance -= entry_cost
                        risk_monitor.add_position_risk(risk_pct * 100)
                        if not quiet:
                            logger.info(f"Открыта {side} позиция {sym} в {current_time}, цена {current_price:.2f}, частей: 1")

            if balance <= 0:
                logger.warning("Баланс исчерпан")
                break

        # Закрытие всех позиций в конце периода
        for sym, pos in list(open_positions.items()):
            last_price = self.data[sym]['close'].iloc[-1]
            balance, removed_risk, pnl = self._close_position(
                sym, pos, balance, self.end_date, last_price, reason='end_of_period'
            )
            risk_monitor.remove_position_risk(removed_risk)
            del open_positions[sym]

        if not quiet:
            self._print_metrics(balance)

    def _try_pyramid_add(self, symbol, pos, current_price, df, params, balance, current_time):
        max_parts = params.get('max_pyramid_parts', 4)
        risk_per_add = params.get('risk_per_add', 0.015)
        total_risk_limit = params.get('total_risk_limit', 0.05)
        parts = pos['parts']
        if len(parts) >= max_parts:
            logger.debug(f"Максимум частей ({max_parts}) уже достигнут для {symbol}")
            return
        total_risk = sum(p['risk'] for p in parts)
        if total_risk + risk_per_add > total_risk_limit:
            logger.debug(f"Превышен лимит общего риска для {symbol}: {total_risk + risk_per_add:.3f} > {total_risk_limit}")
            return
        equity = balance + self._total_unrealised_pnl(pos, current_price)
        entry_cost = equity * risk_per_add / 0.20
        qty = entry_cost / current_price
        if entry_cost < 10 or qty <= 0:
            return
        new_part = {
            'entry_price': current_price,
            'qty': qty,
            'risk': risk_per_add,
            'allocated': entry_cost
        }
        pos['parts'].append(new_part)
        pos['total_risk'] = total_risk + risk_per_add
        pos['avg_price'] = self._calc_avg_price(pos)
        logger.info(f"Добавка {symbol} в {current_time}, цена {current_price:.2f}, частей: {len(parts)}")

    def _calc_avg_price(self, pos):
        parts = pos['parts']
        total_cost = sum(p['entry_price'] * p['qty'] for p in parts)
        total_qty = sum(p['qty'] for p in parts)
        if total_qty > 0:
            return total_cost / total_qty
        return 0

    def _total_unrealised_pnl(self, pos, current_price):
        if pos['side'] == 'long':
            return sum((current_price - p['entry_price']) * p['qty'] for p in pos['parts'])
        else:
            return sum((p['entry_price'] - current_price) * p['qty'] for p in pos['parts'])

    def _check_max_trend_stop(self, sym, pos, df, params):
        trailing_period = params.get('trailing_period', 4)
        if len(df) < trailing_period:
            return False
        if pos['side'] == 'long':
            stop_price = df['low'].tail(trailing_period).min()
            return df['low'].iloc[-1] <= stop_price
        elif pos['side'] == 'short':
            stop_price = df['high'].tail(trailing_period).max()
            return df['high'].iloc[-1] >= stop_price
        return False

    def _close_position(self, sym, pos, balance, close_time, close_price=None, reason='unknown'):
        if close_price is None:
            mask = self.data[sym]['timestamp'] <= close_time
            if mask.sum() > 0:
                close_price = self.data[sym].loc[mask, 'close'].iloc[-1]
            else:
                close_price = self.data[sym]['close'].iloc[-1]

        qty = sum(p['qty'] for p in pos['parts'])
        side = pos['side']
        entry_price = pos['avg_price']
        commission = close_price * qty * TAKER_FEE

        if side == 'long':
            cash_in = qty * close_price
            pnl_gross = (close_price - entry_price) * qty
            balance += cash_in - commission
            pnl_net = pnl_gross - commission
        else:  # short
            cash_out = qty * close_price
            pnl_gross = (entry_price - close_price) * qty
            balance -= cash_out + commission
            pnl_net = pnl_gross - commission

        self.results.append({
            'symbol': sym,
            'entry_time': pos['parts'][0].get('entry_time', close_time),
            'exit_time': close_time,
            'entry_price': entry_price,
            'exit_price': close_price,
            'qty': qty,
            'side': side,
            'pnl': pnl_net,
            'parts': len(pos['parts']),
            'reason': reason
        })

        logger.info(
            f"Закрыта {sym} в {close_time}, цена {close_price:.2f}, "
            f"PnL: {pnl_net:.2f} USDT, причина: {reason}, баланс: {balance:.2f}"
        )
        risk_pct = pos.get('risk_percent', 0.0)
        return balance, risk_pct, pnl_net

    def _print_metrics(self, final_balance):
        total_return = (final_balance - self.start_capital) / self.start_capital * 100
        days = (self.end_date - self.start_date).days
        cagr = ((final_balance / self.start_capital) ** (365 / days) - 1) * 100 if days > 0 else 0
        print("\n=== РЕЗУЛЬТАТЫ БЭКТЕСТА ===")
        print(f"Период: {self.start_date.date()} – {self.end_date.date()} ({days} дн.)")
        print(f"Начальный баланс: {self.start_capital:.2f} USDT")
        print(f"Конечный баланс: {final_balance:.2f} USDT")
        print(f"Общая доходность: {total_return:.2f}%")
        print(f"Годовая доходность (CAGR): {cagr:.2f}%")
        print(f"Всего сделок: {len(self.results)}")
        if self.results:
            profits = [r['pnl'] for r in self.results]
            print(f"Прибыльные сделки: {sum(1 for p in profits if p > 0)}")
            print(f"Убыточные сделки: {sum(1 for p in profits if p <= 0)}")
            print(f"Средний PnL на сделку: {np.mean(profits):.2f} USDT")