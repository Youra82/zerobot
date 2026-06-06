# src/zerobot/analysis/portfolio_simulator.py
import pandas as pd
import numpy as np
from tqdm import tqdm
import sys
import os
import ta
import math
import json

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.strategy.ear_engine import EAREngine
from zerobot.strategy.ear_logic import get_ear_signal
from zerobot.analysis.backtester import load_data


class Bias:
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


def run_portfolio_simulation(start_capital, strategies_data, start_date, end_date, verbose=True):
    """Chronologische Portfolio-Simulation mit mehreren ZeroBot-EAR-Strategien."""
    if verbose:
        print("\n--- Starte Portfolio-Simulation (ZeroBot EAR)... ---")
        print("1/3: Bereite Strategie-Daten vor...")

    processed_strategies = {}
    all_timestamps       = set()

    _iter = tqdm(strategies_data.items(), desc="Verarbeite Strategien") if verbose else strategies_data.items()
    for key, strat in _iter:
        try:
            df = strat['data'].copy()
            if df.empty or len(df) < 100:
                continue

            params = strat.get('smc_params', {})
            if not params:
                params = strat.get('strategy', {})

            atr_indicator = ta.volatility.AverageTrueRange(
                high=df['high'], low=df['low'], close=df['close'], window=14)
            df['atr'] = atr_indicator.average_true_range()

            engine = EAREngine(settings=params)
            df     = engine.process_dataframe(df)

            df.dropna(subset=['atr'], inplace=True)
            if df.empty:
                continue

            processed_strategies[key] = {
                'data':        df,
                'params':      params,
                'risk_params': strat.get('risk_params', {}),
                'symbol':      strat.get('symbol', key),
                'timeframe':   strat.get('timeframe', ''),
            }
            all_timestamps.update(df.index)

        except Exception as e:
            print(f"Fehler bei Vorbereitung von {key}: {e}")

    if not processed_strategies:
        return None

    sorted_timestamps = sorted(list(all_timestamps))
    if verbose:
        print(f"-> {len(sorted_timestamps)} Zeitschritte zu simulieren.")
        print("2/3: Führe Simulation durch...")

    equity             = start_capital
    peak_equity        = start_capital
    max_drawdown_pct   = 0.0
    max_drawdown_date  = None
    min_equity_ever    = start_capital
    liquidation_date   = None

    open_positions = {}
    trade_history  = []
    equity_curve   = []

    fee_pct                       = 0.06 / 100
    max_allowed_effective_leverage = 10
    absolute_max_notional_value    = 1000000
    min_notional                   = 5.0

    _ts_iter = tqdm(sorted_timestamps, desc="Simuliere") if verbose else sorted_timestamps
    for ts in _ts_iter:
        if liquidation_date:
            break

        unrealized_pnl    = 0
        positions_to_close = []

        # A) Offene Positionen managen
        for key, pos in open_positions.items():
            strat = processed_strategies.get(key)
            if not strat or ts not in strat['data'].index:
                if pos.get('last_known_price'):
                    pnl_mult = 1 if pos['side'] == 'long' else -1
                    unrealized_pnl += pos['notional_value'] * \
                        (pos['last_known_price'] / pos['entry_price'] - 1) * pnl_mult
                continue

            current_candle = strat['data'].loc[ts]
            pos['last_known_price'] = current_candle['close']
            exit_price = None

            if pos['side'] == 'long':
                if current_candle['low']  <= pos['stop_loss']:
                    exit_price = pos['stop_loss']
                elif current_candle['high'] >= pos['take_profit']:
                    exit_price = pos['take_profit']
            else:
                if current_candle['high'] >= pos['stop_loss']:
                    exit_price = pos['stop_loss']
                elif current_candle['low']  <= pos['take_profit']:
                    exit_price = pos['take_profit']

            if exit_price:
                pnl_pct  = (exit_price / pos['entry_price'] - 1) \
                           if pos['side'] == 'long' \
                           else (1 - exit_price / pos['entry_price'])
                pnl_usd   = pos['notional_value'] * pnl_pct
                total_fees = pos['notional_value'] * fee_pct * 2
                net_pnl    = pnl_usd - total_fees
                equity    += net_pnl
                trade_history.append({
                    'strategy_key': key,
                    'ts':           ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                    'entry_time':   pos.get('entry_time', ts).isoformat() if hasattr(pos.get('entry_time', ts), 'isoformat') else str(pos.get('entry_time', ts)),
                    'symbol':       pos.get('symbol_key', key),
                    'timeframe':    pos.get('timeframe', ''),
                    'direction':    pos['side'],
                    'entry':        pos['entry_price'],
                    'exit':         exit_price,
                    'pnl':          net_pnl,
                    'leverage':     pos.get('leverage', 0),
                    'margin_used':  round(pos.get('margin_used', 0), 4),
                })
                positions_to_close.append(key)
            else:
                pnl_mult = 1 if pos['side'] == 'long' else -1
                unrealized_pnl += pos['notional_value'] * \
                    (current_candle['close'] / pos['entry_price'] - 1) * pnl_mult

        for key in positions_to_close:
            del open_positions[key]

        # B) Neue Positionen öffnen
        if equity > 0:
            for key, strat in processed_strategies.items():
                if key in open_positions:
                    continue
                if ts not in strat['data'].index:
                    continue

                current_candle   = strat['data'].loc[ts]
                params_for_logic = {"strategy": strat['params'], "risk": strat['risk_params']}
                side, price      = get_ear_signal(strat['data'], current_candle, params_for_logic, Bias.NEUTRAL)

                if side:
                    risk_params  = strat['risk_params']
                    entry_price  = current_candle['close']
                    current_atr  = current_candle['atr']

                    atr_mult = risk_params.get('atr_multiplier_sl', 2.0)
                    min_sl   = risk_params.get('min_sl_pct', 0.5) / 100.0
                    sl_dist  = max(current_atr * atr_mult, entry_price * min_sl)
                    if sl_dist <= 0:
                        continue

                    risk_per_trade = risk_params.get('risk_per_trade_pct', 1.0) / 100.0
                    risk_usd       = equity * risk_per_trade
                    sl_pct         = sl_dist / entry_price
                    if sl_pct <= 0:
                        continue

                    leverage       = risk_params.get('leverage', 10)
                    calc_notional  = risk_usd / sl_pct
                    max_notional   = equity * max_allowed_effective_leverage
                    final_notional = min(calc_notional, max_notional, absolute_max_notional_value)
                    if final_notional < min_notional:
                        continue

                    margin_used         = math.ceil((final_notional / leverage) * 100) / 100
                    current_used_margin = sum(p['margin_used'] for p in open_positions.values())
                    if current_used_margin + margin_used > equity:
                        continue

                    rr    = risk_params.get('risk_reward_ratio', 2.0)
                    act_rr = risk_params.get('trailing_stop_activation_rr', 2.0)

                    if side == 'buy':
                        sl  = entry_price - sl_dist
                        tp  = entry_price + sl_dist * rr
                        act = entry_price + sl_dist * act_rr
                    else:
                        sl  = entry_price + sl_dist
                        tp  = entry_price - sl_dist * rr
                        act = entry_price - sl_dist * act_rr

                    open_positions[key] = {
                        'side':             'long' if side == 'buy' else 'short',
                        'entry_price':      entry_price,
                        'stop_loss':        sl,
                        'take_profit':      tp,
                        'activation_price': act,
                        'trailing_active':  False,
                        'peak_price':       entry_price,
                        'callback_rate':    risk_params.get('trailing_stop_callback_rate_pct', 1.0) / 100,
                        'notional_value':   final_notional,
                        'margin_used':      margin_used,
                        'last_known_price': entry_price,
                        'entry_time':       ts,
                        'symbol_key':       strat.get('symbol', key),
                        'timeframe':        strat.get('timeframe', ''),
                        'leverage':         leverage,
                    }

        # C) Tracking
        current_total_equity = equity + unrealized_pnl
        equity_curve.append({'timestamp': ts, 'equity': current_total_equity})

        peak_equity = max(peak_equity, current_total_equity)
        drawdown    = (peak_equity - current_total_equity) / peak_equity if peak_equity > 0 else 0
        if drawdown > max_drawdown_pct:
            max_drawdown_pct  = drawdown
            max_drawdown_date = ts

        min_equity_ever = min(min_equity_ever, current_total_equity)
        if current_total_equity <= 0 and not liquidation_date:
            liquidation_date = ts

    if verbose:
        print("3/3: Bereite Ergebnisse vor...")

    final_equity  = equity_curve[-1]['equity'] if equity_curve else start_capital
    total_pnl_pct = (final_equity / start_capital - 1) * 100 if start_capital > 0 else 0
    wins          = sum(1 for t in trade_history if t['pnl'] > 0)
    win_rate      = (wins / len(trade_history) * 100) if trade_history else 0

    equity_df = pd.DataFrame(equity_curve)
    if not equity_df.empty:
        equity_df['peak']         = equity_df['equity'].cummax()
        equity_df['drawdown_pct'] = ((equity_df['peak'] - equity_df['equity']) /
                                     equity_df['peak'].replace(0, np.nan)).fillna(0)
        equity_df['timestamp']    = pd.to_datetime(equity_df['timestamp'])
        equity_df.set_index('timestamp', inplace=True, drop=False)

    return {
        "start_capital":    start_capital,
        "end_capital":      final_equity,
        "total_pnl_pct":    total_pnl_pct,
        "trade_count":      len(trade_history),
        "win_rate":         win_rate,
        "max_drawdown_pct": max_drawdown_pct * 100,
        "max_drawdown_date": max_drawdown_date,
        "min_equity":       min_equity_ever,
        "liquidation_date": liquidation_date,
        "trade_history":    trade_history,
        "equity_curve":     equity_df,
    }
