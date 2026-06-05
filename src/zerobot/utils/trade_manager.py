# src/zerobot/utils/trade_manager.py
import json
import logging
import os
import time
from datetime import datetime, timedelta

import ccxt
import numpy as np
import pandas as pd
import ta
import math

from zerobot.strategy.renko_engine import RenkoEngine
from zerobot.strategy.renko_logic import get_renko_signal
from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message
from zerobot.utils.timeframe_utils import determine_htf

PROJECT_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
ARTIFACTS_PATH  = os.path.join(PROJECT_ROOT, 'artifacts')
DB_PATH         = os.path.join(ARTIFACTS_PATH, 'db')
TRADE_LOCK_FILE = os.path.join(DB_PATH, 'trade_lock.json')


class Bias:
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


def determine_market_bias(htf_df):
    if htf_df is None or htf_df.empty or len(htf_df) < 50:
        return Bias.NEUTRAL
    try:
        ema_fast = htf_df['close'].ewm(span=20, adjust=False).mean()
        ema_slow = htf_df['close'].ewm(span=50, adjust=False).mean()
        current_fast = ema_fast.iloc[-1]
        current_slow = ema_slow.iloc[-1]
        distance_pct = abs(current_fast - current_slow) / current_slow
        if current_fast > current_slow and distance_pct > 0.005:
            return Bias.BULLISH
        elif current_fast < current_slow and distance_pct > 0.005:
            return Bias.BEARISH
        return Bias.NEUTRAL
    except Exception:
        return Bias.NEUTRAL


def load_or_create_trade_lock():
    os.makedirs(DB_PATH, exist_ok=True)
    if os.path.exists(TRADE_LOCK_FILE):
        with open(TRADE_LOCK_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_trade_lock(trade_lock):
    with open(TRADE_LOCK_FILE, 'w') as f:
        json.dump(trade_lock, f, indent=4)


def is_trade_locked(symbol_timeframe):
    trade_lock    = load_or_create_trade_lock()
    lock_time_str = trade_lock.get(symbol_timeframe)
    if lock_time_str:
        lock_time = datetime.strptime(lock_time_str, "%Y-%m-%d %H:%M:%S")
        if datetime.now() < lock_time:
            return True
    return False


def set_trade_lock(symbol_timeframe, lock_duration_minutes=60):
    lock_time  = datetime.now() + timedelta(minutes=lock_duration_minutes)
    trade_lock = load_or_create_trade_lock()
    trade_lock[symbol_timeframe] = lock_time.strftime("%Y-%m-%d %H:%M:%S")
    save_trade_lock(trade_lock)


def housekeeper_routine(exchange, symbol, logger):
    try:
        logger.info(f"Housekeeper: Starte Aufräumroutine für {symbol}...")
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(2)

        position = exchange.fetch_open_positions(symbol)
        if position:
            pos_info   = position[0]
            close_side = 'sell' if pos_info['side'] == 'long' else 'buy'
            logger.warning(f"Housekeeper: Schließe verwaiste Position ({pos_info['side']} {pos_info['contracts']})...")
            exchange.create_market_order(symbol, close_side, float(pos_info['contracts']), {'reduceOnly': True})
            time.sleep(3)

        if exchange.fetch_open_positions(symbol):
            logger.error("Housekeeper: Position konnte nicht geschlossen werden!")
        else:
            logger.info(f"Housekeeper: {symbol} ist jetzt sauber.")
        return True
    except Exception as e:
        logger.error(f"Housekeeper-Fehler: {e}", exc_info=True)
        return False


def check_and_open_new_position(exchange, model, scaler, params, telegram_config, logger):
    symbol          = params['market']['symbol']
    timeframe       = params['market']['timeframe']
    symbol_timeframe = f"{symbol.replace('/', '-')}_{timeframe}"

    if is_trade_locked(symbol_timeframe):
        logger.info(f"Trade für {symbol_timeframe} gesperrt – überspringe.")
        return

    try:
        logger.info(f"Prüfe ZeroBot (Renko) Signal für {symbol} ({timeframe})...")

        recent_data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=1000)
        if recent_data.empty or len(recent_data) < 50:
            logger.warning(f"Nicht genügend OHLCV-Daten (gefunden: {len(recent_data)}) – überspringe.")
            return

        # ATR berechnen
        strat_params  = params.get('strategy', {})
        atr_indicator = ta.volatility.AverageTrueRange(
            high=recent_data['high'], low=recent_data['low'],
            close=recent_data['close'], window=14)
        recent_data['atr'] = atr_indicator.average_true_range()

        # MTF Bias
        htf          = params['market'].get('htf')
        market_bias  = Bias.NEUTRAL
        if htf:
            try:
                htf_data = exchange.fetch_recent_ohlcv(symbol, htf, limit=100)
                if not htf_data.empty:
                    market_bias = determine_market_bias(htf_data)
                    logger.info(f"HTF ({htf}) Bias: {market_bias}")
            except Exception as e:
                logger.warning(f"HTF-Daten konnten nicht abgerufen werden: {e}")

        # Renko Engine
        engine         = RenkoEngine(settings=strat_params)
        processed_data = engine.process_dataframe(recent_data)
        current_candle = processed_data.iloc[-1]

        signal_side, signal_price = get_renko_signal(processed_data, current_candle, params, market_bias)

        if not signal_side:
            logger.info("Kein Renko-Signal – überspringe.")
            return

        # Re-Entry-Schutz
        last_entry_key = f"{symbol_timeframe}_last_entry_price"
        trade_lock     = load_or_create_trade_lock()
        last_entry_price = trade_lock.get(last_entry_key)
        if last_entry_price:
            try:
                last_price    = float(last_entry_price)
                current_price = signal_price or exchange.fetch_ticker(symbol)['last']
                distance_pct  = abs(current_price - last_price) / last_price
                if distance_pct < 0.015:
                    logger.info(f"Re-Entry-Schutz: Preis zu nah ({distance_pct*100:.2f}%) – überspringe.")
                    return
            except (ValueError, TypeError):
                pass

        if exchange.fetch_open_positions(symbol):
            logger.info("Position bereits offen – überspringe.")
            return

        # Risk Management
        risk_params  = params.get('risk', {})
        leverage     = risk_params.get('leverage', 10)
        margin_mode  = risk_params.get('margin_mode', 'isolated')

        exchange.set_margin_mode(symbol, margin_mode)
        exchange.set_leverage(symbol, leverage)

        balance = exchange.fetch_balance_usdt()
        if balance <= 0:
            logger.error("Kein USDT-Guthaben.")
            return

        ticker      = exchange.fetch_ticker(symbol)
        entry_price = signal_price or ticker['last']

        base_rr            = risk_params.get('risk_reward_ratio', 2.0)
        risk_pct           = risk_params.get('risk_per_trade_pct', 1.0) / 100.0
        risk_usdt          = balance * risk_pct
        atr_multiplier_sl  = risk_params.get('atr_multiplier_sl', 2.0)
        min_sl_pct         = risk_params.get('min_sl_pct', 0.3) / 100.0

        current_atr = current_candle.get('atr')

        # Adaptive RR
        if not pd.isna(current_atr) and current_atr > 0:
            atr_avg = processed_data['atr'].tail(50).mean()
            if current_atr > atr_avg * 1.5:
                rr = min(base_rr * 1.3, 5.0)
                logger.info(f"High Vol – RR erhöht auf {rr:.2f}")
            elif current_atr < atr_avg * 0.7:
                rr = max(base_rr * 0.8, 1.5)
                logger.info(f"Low Vol – RR gesenkt auf {rr:.2f}")
            else:
                rr = base_rr
        else:
            rr = base_rr

        if pd.isna(current_atr) or current_atr <= 0:
            sl_distance = entry_price * min_sl_pct
        else:
            sl_distance = max(current_atr * atr_multiplier_sl, entry_price * min_sl_pct)

        if sl_distance <= 0:
            return

        if signal_side == 'buy':
            sl_price  = entry_price - sl_distance
            tp_price  = entry_price + sl_distance * rr
            pos_side  = 'buy'
            tsl_side  = 'sell'
        else:
            sl_price  = entry_price + sl_distance
            tp_price  = entry_price - sl_distance * rr
            pos_side  = 'sell'
            tsl_side  = 'buy'

        sl_pct_equiv      = sl_distance / entry_price
        calc_notional     = risk_usdt / sl_pct_equiv
        amount            = calc_notional / entry_price

        min_amount = exchange.markets[symbol].get('limits', {}).get('amount', {}).get('min', 0.0)
        if amount < min_amount:
            logger.error(f"Ordergröße {amount} < Mindestbetrag {min_amount}.")
            return

        logger.info(f"Eröffne {pos_side.upper()}-Position: {amount:.6f} @ ${entry_price:.6f} | Risk: {risk_usdt:.2f} USDT")

        entry_order = exchange.create_market_order(
            symbol, pos_side, amount,
            {'leverage': leverage, 'marginMode': margin_mode})

        if not entry_order:
            return

        time.sleep(2)
        position = exchange.fetch_open_positions(symbol)
        if not position:
            return

        pos_info  = position[0]
        contracts = float(pos_info['contracts'])

        sl_rounded = float(exchange.exchange.price_to_precision(symbol, sl_price))
        tp_rounded = float(exchange.exchange.price_to_precision(symbol, tp_price))
        exchange.place_trigger_market_order(symbol, tsl_side, contracts, sl_rounded, {'reduceOnly': True})

        act_rr        = risk_params.get('trailing_stop_activation_rr', 1.5)
        callback_pct  = risk_params.get('trailing_stop_callback_rate_pct', 0.5) / 100.0

        act_price = (entry_price + sl_distance * act_rr) if pos_side == 'buy' \
                    else (entry_price - sl_distance * act_rr)
        exchange.place_trailing_stop_order(symbol, tsl_side, contracts, act_price, callback_pct, {'reduceOnly': True})

        set_trade_lock(symbol_timeframe)

        trade_lock = load_or_create_trade_lock()
        trade_lock[f"{symbol_timeframe}_last_entry_price"] = entry_price
        save_trade_lock(trade_lock)

        if telegram_config and telegram_config.get('bot_token') and telegram_config.get('chat_id'):
            msg = (
                f"ZEROBOT (Renko): {symbol} ({timeframe})\n"
                f"- Richtung: {pos_side.upper()}\n"
                f"- Entry: ${entry_price:.6f}\n"
                f"- SL: ${sl_rounded:.6f}\n"
                f"- TP: ${tp_rounded:.6f}"
            )
            send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)

        logger.info("Trade-Eröffnung erfolgreich abgeschlossen.")

    except ccxt.InsufficientFunds as e:
        logger.error(f"InsufficientFunds: {e}")
    except Exception as e:
        logger.error(f"Unerwarteter Fehler: {e}", exc_info=True)
        housekeeper_routine(exchange, symbol, logger)


def full_trade_cycle(exchange, model, scaler, params, telegram_config, logger):
    symbol = params['market']['symbol']
    try:
        pos = exchange.fetch_open_positions(symbol)
        if pos:
            logger.info(f"Position offen – Management via SL/TP/TSL.")
        else:
            housekeeper_routine(exchange, symbol, logger)
            check_and_open_new_position(exchange, model, scaler, params, telegram_config, logger)
    except Exception as e:
        logger.error(f"Fehler im Zyklus: {e}", exc_info=True)
        time.sleep(5)
