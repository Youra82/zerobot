# src/zerobot/analysis/backtester.py
import os
import json
import sys
import math
from collections import defaultdict

import pandas as pd
import numpy as np
import ta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.strategy.ear_engine import EAREngine
from zerobot.strategy.ear_logic import get_ear_signal
from zerobot.utils.timeframe_utils import determine_htf

secrets_cache = None


def load_active_configs():
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
    active_pairs = None
    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
            s = json.load(f)
        entries = s.get('live_trading_settings', {}).get('active_strategies', [])
        active_pairs = set()
        for entry in entries:
            if not entry.get('active', True):
                continue
            sym = entry.get('symbol', '').strip()
            tf  = entry.get('timeframe', '').strip()
            if sym and tf:
                active_pairs.add((sym, tf))
    except Exception:
        active_pairs = None

    result = []
    if os.path.isdir(configs_dir):
        for fn in sorted(os.listdir(configs_dir)):
            if fn.startswith('config_') and fn.endswith('.json'):
                try:
                    with open(os.path.join(configs_dir, fn)) as f:
                        cfg = json.load(f)
                    sym = cfg.get('market', {}).get('symbol', '')
                    tf  = cfg.get('market', {}).get('timeframe', '')
                    if active_pairs is None or (sym, tf) in active_pairs:
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
            data_start    = data.index.min()
            data_end      = data.index.max()
            req_start     = pd.to_datetime(start_date_str, utc=True)
            req_end       = pd.to_datetime(end_date_str, utc=True)
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
            req_start_dt = pd.to_datetime(start_date_str, utc=True)
            req_end_dt   = pd.to_datetime(end_date_str, utc=True)
            buffer_dt    = req_start_dt - pd.Timedelta(days=20)
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

    # EAR Engine — Signale + Bricks
    engine         = EAREngine(settings=strategy_params)
    processed_data = engine.process_dataframe(data)

    # Bricks vorberechnen (für SL = Entry-Brick-Open und TP = erster Gegenbrick)
    bricks = engine._build_bricks(data)
    bricks_by_candle         = defaultdict(list)
    last_brick_idx_at_candle = {}
    for bidx, brick in enumerate(bricks):
        cidx = brick['candle_idx']
        bricks_by_candle[cidx].append(brick)
        last_brick_idx_at_candle[cidx] = bidx

    current_capital  = start_capital
    peak_capital     = start_capital
    max_drawdown_pct = 0.0
    trades_count     = 0
    wins_count       = 0
    position         = None

    risk_per_trade_pct    = risk_params.get('risk_per_trade_pct', 1.0) / 100
    leverage              = risk_params.get('leverage', 10)
    fee_pct               = (fee_pct_override / 100) if fee_pct_override is not None else (0.06 / 100)
    absolute_max_notional = 1000000

    trades_list      = []
    params_for_logic = {"strategy": strategy_params, "risk": risk_params}

    for i, (timestamp, current_candle) in enumerate(processed_data.iterrows()):
        if current_capital <= 0:
            break

        candle_bricks = bricks_by_candle.get(i, [])

        # ── Positions-Management ──────────────────────────────────────────────
        if position:
            exit_price  = None
            exit_reason = None

            # 1. Candle-Level SL (fester Preis = Open des Entry-Bricks)
            if position['side'] == 'long':
                if current_candle['low'] <= position['stop_loss']:
                    exit_price  = position['stop_loss']
                    exit_reason = 'sl'
            else:
                if current_candle['high'] >= position['stop_loss']:
                    exit_price  = position['stop_loss']
                    exit_reason = 'sl'

            # 2. Brick-Level TP: erster vollständiger Brick in Gegenrichtung
            if exit_price is None:
                for brick in candle_bricks:
                    if position['side'] == 'long' and brick['direction'] == 'down':
                        exit_price  = brick['close']
                        exit_reason = 'tp'
                        break
                    elif position['side'] == 'short' and brick['direction'] == 'up':
                        exit_price  = brick['close']
                        exit_reason = 'tp'
                        break

            if exit_price:
                pnl_pct        = (exit_price / position['entry_price'] - 1) \
                                  if position['side'] == 'long' \
                                  else (1 - exit_price / position['entry_price'])
                notional_value = position['notional_value']
                pnl_usd        = notional_value * pnl_pct
                total_fees     = notional_value * fee_pct * 2
                net            = pnl_usd - total_fees
                current_capital += net
                if net > 0:
                    wins_count += 1
                trades_count += 1
                if return_trades:
                    trades_list.append({
                        'entry_time':    str(position.get('entry_time', '')),
                        'exit_time':     str(timestamp),
                        'side':          position['side'],
                        'entry_price':   position['entry_price'],
                        'exit_price':    exit_price,
                        'stop_loss':     position['stop_loss'],
                        'take_profit':   None,
                        'exit_reason':   exit_reason,
                        'pnl_usd':       round(net, 4),
                        'win':           net > 0,
                        'capital_after': round(current_capital, 4),
                    })
                position     = None
                peak_capital = max(peak_capital, current_capital)
                if peak_capital > 0:
                    drawdown         = (peak_capital - current_capital) / peak_capital
                    max_drawdown_pct = max(max_drawdown_pct, drawdown)
                continue

        # ── Einstiegs-Logik ───────────────────────────────────────────────────
        if not position and current_capital > 0:
            side, price = get_ear_signal(processed_data, current_candle, params_for_logic, Bias.NEUTRAL)

            if side:
                # Entry = letzter abgeschlossener Brick dieser Candle (nicht candle['close'],
                # da mehrere Bricks pro Candle entstehen können und candle close ≠ brick close)
                bidx = last_brick_idx_at_candle.get(i)
                if bidx is not None and bidx > 0:
                    entry_price = bricks[bidx]['close']        # letzter Brick-Level
                    sl_price    = bricks[bidx - 1]['close']    # vorheriger Brick = SL (1 Brick Abstand)
                else:
                    entry_price = current_candle['close']
                    sl_price    = entry_price * (0.99 if side == 'buy' else 1.01)

                sl_dist = abs(entry_price - sl_price)
                if sl_dist <= 0:
                    continue

                risk_amount    = current_capital * risk_per_trade_pct
                sl_pct         = sl_dist / entry_price
                calc_notional  = risk_amount / sl_pct
                max_notional   = current_capital * leverage
                final_notional = min(calc_notional, max_notional, absolute_max_notional)

                margin_needed = final_notional / leverage
                if margin_needed > current_capital:
                    continue

                position = {
                    'side':           'long' if side == 'buy' else 'short',
                    'entry_price':    entry_price,
                    'stop_loss':      sl_price,
                    'take_profit':    None,
                    'margin_used':    margin_needed,
                    'notional_value': final_notional,
                    'entry_time':     timestamp,
                }

    win_rate      = (wins_count / trades_count * 100) if trades_count > 0 else 0
    final_pnl_pct = ((current_capital - start_capital) / start_capital) * 100
    final_capital = max(0, current_capital)

    result = {
        "total_pnl_pct":    final_pnl_pct,
        "trades_count":     trades_count,
        "win_rate":         win_rate,
        "max_drawdown_pct": max_drawdown_pct,
        "end_capital":      final_capital,
    }
    if return_trades:
        result['trades'] = trades_list
    return result
