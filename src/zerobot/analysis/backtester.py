# src/zerobot/analysis/backtester.py
import os
import pandas as pd
import numpy as np
import json
import sys
from tqdm import tqdm
import ta
import math

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.strategy.renko_engine import RenkoEngine
from zerobot.strategy.renko_logic import get_renko_signal
from zerobot.utils.timeframe_utils import determine_htf

secrets_cache = None


def load_active_configs():
    """Load configs filtered to active_strategies in settings.json.
    Falls back to ALL configs if settings.json is missing or has no active_strategies."""
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')

    # Read active strategies from settings.json
    active_pairs = set()
    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
            s = json.load(f)
        for entry in s.get('live_trading_settings', {}).get('active_strategies', []):
            sym = entry.get('symbol', '').strip()
            tf  = entry.get('timeframe', '').strip()
            if sym and tf:
                active_pairs.add((sym, tf))
    except Exception:
        pass  # fall through to "all configs" behaviour

    result = []
    if os.path.isdir(configs_dir):
        for fn in sorted(os.listdir(configs_dir)):
            if fn.startswith('config_') and fn.endswith('.json'):
                try:
                    with open(os.path.join(configs_dir, fn)) as f:
                        cfg = json.load(f)
                    sym = cfg.get('market', {}).get('symbol', '')
                    tf  = cfg.get('market', {}).get('timeframe', '')
                    if not active_pairs or (sym, tf) in active_pairs:
                        result.append((fn, cfg))
                except Exception:
                    pass
    return result


class Bias:
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


def load_data(symbol, timeframe, start_date_str, end_date_str):
    global secrets_cache
    data_dir   = os.path.join(PROJECT_ROOT, 'data')
    cache_dir  = os.path.join(data_dir, 'cache')
    sym_file   = symbol.replace('/', '-').replace(':', '-')
    cache_file = os.path.join(cache_dir, f"{sym_file}_{timeframe}.csv")

    try:
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return pd.DataFrame()

    if os.path.exists(cache_file):
        try:
            data = pd.read_csv(cache_file, index_col='timestamp', parse_dates=True)
            if not isinstance(data.index, pd.DatetimeIndex):
                data.index = pd.to_datetime(data.index, utc=True)
            data_start   = data.index.min()
            data_end     = data.index.max()
            req_start    = pd.to_datetime(start_date_str, utc=True)
            req_end      = pd.to_datetime(end_date_str, utc=True)
            req_start_buf = req_start - pd.Timedelta(days=20)
            if data_start <= req_start_buf and data_end >= req_end:
                return data.loc[req_start_buf:req_end]
        except Exception:
            try:
                os.remove(cache_file)
            except OSError:
                pass

    try:
        if secrets_cache is None:
            with open(os.path.join(PROJECT_ROOT, 'secret.json'), "r") as f:
                secrets_cache = json.load(f)

        api_setup = None
        for key in ('zerobot', 'stbot', 'utbot2', 'titanbot'):
            if key in secrets_cache:
                api_setup = secrets_cache[key][0]
                break

        if not api_setup:
            return pd.DataFrame()

        exchange = Exchange(api_setup)
        if not exchange.markets:
            return pd.DataFrame()

        start_dt  = pd.to_datetime(start_date_str, utc=True) - pd.Timedelta(days=30)
        full_data = exchange.fetch_historical_ohlcv(
            symbol, timeframe, start_dt.strftime('%Y-%m-%d'), end_date_str)

        if not full_data.empty:
            full_data.to_csv(cache_file)
            req_start_dt  = pd.to_datetime(start_date_str, utc=True)
            req_end_dt    = pd.to_datetime(end_date_str, utc=True)
            buffer_dt     = req_start_dt - pd.Timedelta(days=20)
            return full_data.loc[buffer_dt:req_end_dt]
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def run_backtest(data, strategy_params, risk_params, start_capital=1000, verbose=False,
                 fee_pct_override=None, return_trades=False):
    if data.empty or len(data) < 100:
        return {"total_pnl_pct": -100, "trades_count": 0, "win_rate": 0,
                "max_drawdown_pct": 1.0, "end_capital": start_capital}

    # ATR berechnen
    try:
        atr_indicator = ta.volatility.AverageTrueRange(
            high=data['high'], low=data['low'], close=data['close'], window=14)
        data['atr'] = atr_indicator.average_true_range()
        data.dropna(subset=['atr'], inplace=True)
    except Exception:
        return {"total_pnl_pct": -100, "end_capital": start_capital}

    # Renko Engine
    engine         = RenkoEngine(settings=strategy_params)
    processed_data = engine.process_dataframe(data)

    current_capital         = start_capital
    peak_capital            = start_capital
    max_drawdown_pct        = 0.0
    trades_count            = 0
    wins_count              = 0
    position                = None

    risk_reward_ratio    = risk_params.get('risk_reward_ratio', 2.0)
    risk_per_trade_pct   = risk_params.get('risk_per_trade_pct', 1.0) / 100
    activation_rr        = risk_params.get('trailing_stop_activation_rr', 2.0)
    leverage             = risk_params.get('leverage', 10)
    fee_pct              = (fee_pct_override / 100) if fee_pct_override is not None else (0.06 / 100)
    atr_multiplier_sl    = risk_params.get('atr_multiplier_sl', 2.0)
    min_sl_pct           = risk_params.get('min_sl_pct', 0.3) / 100.0
    absolute_max_notional = 1000000

    trades_list = []
    params_for_logic = {"strategy": strategy_params, "risk": risk_params}

    for timestamp, current_candle in processed_data.iterrows():
        if current_capital <= 0:
            break

        # Positions-Management
        if position:
            exit_price = None
            if position['side'] == 'long':
                if current_candle['low']  <= position['stop_loss']:
                    exit_price = position['stop_loss']
                elif current_candle['high'] >= position['take_profit']:
                    exit_price = position['take_profit']
            elif position['side'] == 'short':
                if current_candle['high'] >= position['stop_loss']:
                    exit_price = position['stop_loss']
                elif current_candle['low']  <= position['take_profit']:
                    exit_price = position['take_profit']

            if exit_price:
                pnl_pct       = (exit_price / position['entry_price'] - 1) \
                                if position['side'] == 'long' \
                                else (1 - exit_price / position['entry_price'])
                notional_value = position['notional_value']
                pnl_usd        = notional_value * pnl_pct
                total_fees     = notional_value * fee_pct * 2
                current_capital += (pnl_usd - total_fees)
                if (pnl_usd - total_fees) > 0:
                    wins_count += 1
                trades_count += 1
                if return_trades:
                    trades_list.append({
                        'timestamp': str(timestamp),
                        'side': position['side'],
                        'pnl_usd': round(pnl_usd - total_fees, 4),
                        'win': (pnl_usd - total_fees) > 0,
                    })
                position      = None
                peak_capital  = max(peak_capital, current_capital)
                if peak_capital > 0:
                    drawdown         = (peak_capital - current_capital) / peak_capital
                    max_drawdown_pct = max(max_drawdown_pct, drawdown)
                continue

        # Einstiegs-Logik
        if not position and current_capital > 0:
            side, price = get_renko_signal(processed_data, current_candle, params_for_logic, Bias.NEUTRAL)

            if side:
                entry_price = current_candle['close']
                current_atr = current_candle.get('atr', 0)
                if current_atr <= 0:
                    continue

                sl_dist      = max(current_atr * atr_multiplier_sl, entry_price * min_sl_pct)
                risk_amount  = current_capital * risk_per_trade_pct
                sl_pct       = sl_dist / entry_price
                if sl_pct <= 0:
                    continue

                calc_notional = risk_amount / sl_pct
                max_notional  = current_capital * 10
                final_notional = min(calc_notional, max_notional, absolute_max_notional)

                margin_needed = final_notional / leverage
                if margin_needed > current_capital:
                    continue

                if side == 'buy':
                    sl  = entry_price - sl_dist
                    tp  = entry_price + sl_dist * risk_reward_ratio
                else:
                    sl  = entry_price + sl_dist
                    tp  = entry_price - sl_dist * risk_reward_ratio

                position = {
                    'side':           'long' if side == 'buy' else 'short',
                    'entry_price':    entry_price,
                    'stop_loss':      sl,
                    'take_profit':    tp,
                    'margin_used':    margin_needed,
                    'notional_value': final_notional,
                }

    win_rate      = (wins_count / trades_count * 100) if trades_count > 0 else 0
    final_pnl_pct = ((current_capital - start_capital) / start_capital) * 100 if start_capital > 0 else 0
    final_capital = max(0, current_capital)

    result = {
        "total_pnl_pct":   final_pnl_pct,
        "trades_count":    trades_count,
        "win_rate":        win_rate,
        "max_drawdown_pct": max_drawdown_pct,
        "end_capital":     final_capital,
    }
    if return_trades:
        result['trades'] = trades_list
    return result
