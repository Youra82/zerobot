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

from zerobot.strategy.ear_engine import EAREngine
from zerobot.strategy.ear_logic import get_ear_signal
from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message
from zerobot.utils.timeframe_utils import determine_htf

PROJECT_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
ARTIFACTS_PATH  = os.path.join(PROJECT_ROOT, 'artifacts')
DB_PATH         = os.path.join(ARTIFACTS_PATH, 'db')
TRADE_LOCK_FILE = os.path.join(DB_PATH, 'trade_lock.json')


def _brick_state_path(symbol_timeframe: str) -> str:
    safe = symbol_timeframe.replace('/', '-').replace(':', '-')
    return os.path.join(DB_PATH, f'ear_brick_state_{safe}.json')


def load_brick_state(symbol_timeframe: str) -> dict:
    """Laedt persistierten EAR-Brick-State (lc, direction) oder leeres Dict."""
    path = _brick_state_path(symbol_timeframe)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_brick_state(symbol_timeframe: str, lc: float, direction: str):
    """Speichert letzten Brick-Level und Richtung fuer naechsten Lauf."""
    os.makedirs(DB_PATH, exist_ok=True)
    with open(_brick_state_path(symbol_timeframe), 'w') as f:
        json.dump({'lc': lc, 'direction': direction}, f)


class Bias:
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


def determine_market_bias(htf_df):
    if htf_df is None or htf_df.empty or len(htf_df) < 50:
        return Bias.NEUTRAL
    try:
        ema_fast     = htf_df['close'].ewm(span=20, adjust=False).mean()
        ema_slow     = htf_df['close'].ewm(span=50, adjust=False).mean()
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
            pos_info = position[0]
            logger.warning(f"Housekeeper: Schließe verwaiste Position ({pos_info['side']} {pos_info['contracts']})...")
            exchange.flash_close_position(symbol)
            time.sleep(3)

        if exchange.fetch_open_positions(symbol):
            logger.error("Housekeeper: Position konnte nicht geschlossen werden!")
        else:
            logger.info(f"Housekeeper: {symbol} ist jetzt sauber.")
        return True
    except Exception as e:
        logger.error(f"Housekeeper-Fehler: {e}", exc_info=True)
        return False


def _close_position(exchange, symbol, pos_info, params, telegram_config, logger, reason='brick_reversal'):
    """Schließt eine offene Position per Market Order."""
    try:
        contracts  = float(pos_info.get('contracts', 0))
        pos_side   = pos_info.get('side', '').lower()
        close_side = 'sell' if pos_side == 'long' else 'buy'

        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(1)

        if contracts <= 0:
            logger.warning(f"_close_position: contracts={contracts}, überspringe.")
            return

        exchange.flash_close_position(symbol)
        logger.info(f"Position geschlossen ({reason}): {pos_side.upper()} {contracts} {symbol}")

        if telegram_config and telegram_config.get('bot_token') and telegram_config.get('chat_id'):
            try:
                ticker     = exchange.fetch_ticker(symbol)
                exit_price = ticker['last']
                tf         = params['market']['timeframe']
                msg = (
                    f"ZEROBOT (EAR) – Position geschlossen\n"
                    f"- Symbol: {symbol} ({tf})\n"
                    f"- Seite: {pos_side.upper()}\n"
                    f"- Exit: ${exit_price:.6f}\n"
                    f"- Grund: {reason}"
                )
                send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Fehler beim Schließen der Position: {e}", exc_info=True)


def check_and_close_on_brick_reversal(exchange, pos_info, params, telegram_config, logger):
    """
    Prüft ob ein EAR-Brick in Gegenrichtung entstanden ist (seit Trade-Eröffnung).
    Falls ja → Position per Market Order schließen (Brick-TP-Exit).
    """
    symbol          = params['market']['symbol']
    timeframe       = params['market']['timeframe']
    symbol_timeframe = f"{symbol.replace('/', '-')}_{timeframe}"
    pos_side        = pos_info.get('side', '').lower()  # 'long' or 'short'

    try:
        recent_data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=500)
        if recent_data.empty or len(recent_data) < 20:
            logger.warning("Nicht genug Daten für Brick-Reversal-Check.")
            return

        strat_params  = params.get('strategy', {})
        atr_indicator = ta.volatility.AverageTrueRange(
            high=recent_data['high'], low=recent_data['low'],
            close=recent_data['close'], window=14)
        recent_data['atr'] = atr_indicator.average_true_range()
        recent_data.dropna(subset=['atr'], inplace=True)

        engine = EAREngine(settings=strat_params)
        brick_state = load_brick_state(symbol_timeframe)
        bricks = engine._build_bricks(
            recent_data,
            init_lc=brick_state.get('lc'),
            init_direction=brick_state.get('direction'),
        )
        if not bricks:
            logger.info("Keine Bricks berechnet – Position hält.")
            return
        save_brick_state(symbol_timeframe, bricks[-1]['close'], bricks[-1]['direction'])

        # Brick-Zähler beim Entry aus trade_lock lesen (wie Backtester: Bricks nach Entry-Brick-Index)
        trade_lock        = load_or_create_trade_lock()
        entry_brick_count = trade_lock.get(f"{symbol_timeframe}_entry_brick_count")
        entry_time_str    = trade_lock.get(f"{symbol_timeframe}_entry_time")

        if entry_brick_count is not None:
            post_entry_bricks = bricks[int(entry_brick_count):]
        else:
            post_entry_bricks = bricks[-10:]  # Fallback

        if not post_entry_bricks:
            logger.info("Noch keine neuen Bricks seit Entry – Position hält.")
            return

        # Prüfe ob Gegenbrick vorhanden
        for brick in post_entry_bricks:
            if pos_side == 'long' and brick['direction'] == 'down':
                logger.info(f"Brick-Reversal: DOWN-Brick nach Long-Entry → schließe Position.")
                _close_position(exchange, symbol, pos_info, params, telegram_config, logger, 'brick_reversal_down')
                return
            elif pos_side == 'short' and brick['direction'] == 'up':
                logger.info(f"Brick-Reversal: UP-Brick nach Short-Entry → schließe Position.")
                _close_position(exchange, symbol, pos_info, params, telegram_config, logger, 'brick_reversal_up')
                return

        logger.info(f"Kein Gegenbrick seit Entry ({entry_time_str}) – Position hält ({pos_side}).")

    except Exception as e:
        logger.error(f"Fehler bei Brick-Reversal-Check: {e}", exc_info=True)


def check_and_open_new_position(exchange, model, scaler, params, telegram_config, logger):
    symbol           = params['market']['symbol']
    timeframe        = params['market']['timeframe']
    symbol_timeframe = f"{symbol.replace('/', '-')}_{timeframe}"

    if is_trade_locked(symbol_timeframe):
        logger.info(f"Trade für {symbol_timeframe} gesperrt – überspringe.")
        return

    try:
        logger.info(f"Prüfe ZeroBot (EAR) Signal für {symbol} ({timeframe})...")

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
        recent_data.dropna(subset=['atr'], inplace=True)

        # EAR Engine — mit persistiertem Brick-State (pfadunabhaengig)
        engine      = EAREngine(settings=strat_params)
        brick_state = load_brick_state(symbol_timeframe)
        init_lc     = brick_state.get('lc')
        init_dir    = brick_state.get('direction')

        processed_data = engine.process_dataframe(
            recent_data, init_lc=init_lc, init_direction=init_dir)
        current_candle = processed_data.iloc[-1]

        signal_side, signal_price = get_ear_signal(processed_data, current_candle, params, Bias.NEUTRAL)

        if not signal_side:
            # State nach jedem Lauf speichern (auch wenn kein Signal)
            raw_bricks = engine._build_bricks(
                recent_data, init_lc=init_lc, init_direction=init_dir)
            if raw_bricks:
                save_brick_state(symbol_timeframe,
                                 raw_bricks[-1]['close'], raw_bricks[-1]['direction'])
            logger.info("Kein EAR-Signal – überspringe.")
            return

        # Re-Entry-Schutz
        last_entry_key   = f"{symbol_timeframe}_last_entry_price"
        trade_lock       = load_or_create_trade_lock()
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
        risk_params = params.get('risk', {})
        leverage    = risk_params.get('leverage', 10)
        margin_mode = risk_params.get('margin_mode', 'isolated')

        exchange.set_margin_mode(symbol, margin_mode)
        exchange.set_leverage(symbol, leverage)

        balance = exchange.fetch_balance_usdt()
        if balance <= 0:
            logger.error("Kein USDT-Guthaben.")
            return

        # Entry = letzter Brick-Close (wie im Backtester: bricks[bidx]['close'])
        # SL    = vorheriger Brick-Close (wie im Backtester: bricks[bidx-1]['close'])
        bricks = engine._build_bricks(recent_data, init_lc=init_lc, init_direction=init_dir)
        if bricks:
            save_brick_state(symbol_timeframe, bricks[-1]['close'], bricks[-1]['direction'])
        if bricks and len(bricks) > 1:
            entry_price = bricks[-1]['close']   # letzter Brick-Level (= Backtester-Entry)
            sl_price    = bricks[-2]['close']   # vorheriger Brick-Level (= Backtester-SL)
        else:
            ticker      = exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
            sl_price    = entry_price * (0.99 if signal_side == 'buy' else 1.01)

        sl_dist = abs(entry_price - sl_price)
        if sl_dist <= 0:
            logger.error("SL-Abstand = 0, überspringe.")
            return

        risk_pct      = risk_params.get('risk_per_trade_pct', 1.0) / 100.0
        risk_usdt     = balance * risk_pct
        sl_pct_equiv  = sl_dist / entry_price
        calc_notional  = risk_usdt / sl_pct_equiv
        max_notional   = balance * leverage * 0.98  # 2% Puffer für Eröffnungsgebühren
        final_notional = min(calc_notional, max_notional, 1_000_000)
        amount         = final_notional / entry_price

        min_amount = exchange.markets[symbol].get('limits', {}).get('amount', {}).get('min', 0.0)
        if amount < min_amount:
            logger.error(f"Ordergröße {amount} < Mindestbetrag {min_amount}.")
            return

        if signal_side == 'buy':
            pos_side  = 'buy'
            tsl_side  = 'sell'
        else:
            pos_side  = 'sell'
            tsl_side  = 'buy'

        sl_rounded = float(exchange.exchange.price_to_precision(symbol, sl_price))

        logger.info(
            f"Eröffne {pos_side.upper()}-Position: {amount:.6f} @ ${entry_price:.6f} | "
            f"SL: ${sl_rounded:.6f} ({sl_dist/entry_price*100:.3f}%) | Risk: {risk_usdt:.2f} USDT"
        )

        # tradeSide: 'open' = Bitget v2 One-Way-Modus (kein holdSide/Hedge-Mode)
        entry_order = exchange.create_market_order(symbol, pos_side, amount, {'tradeSide': 'open'})

        if not entry_order:
            return

        time.sleep(2)
        position = exchange.fetch_open_positions(symbol)
        if not position:
            return

        pos_info  = position[0]
        contracts = float(pos_info['contracts'])

        # Nur SL-Order auf der Börse platzieren — TP wird durch Brick-Reversal-Check ausgelöst
        # tradeSide: 'close' = Bitget v2 One-Way-Modus für schließende Orders
        exchange.place_trigger_market_order(symbol, tsl_side, contracts, sl_rounded,
                                            {'reduceOnly': True, 'tradeSide': 'close'})

        # Entry-Zeit und Seite für Brick-Reversal-Check speichern
        set_trade_lock(symbol_timeframe)
        trade_lock = load_or_create_trade_lock()
        trade_lock[f"{symbol_timeframe}_last_entry_price"] = entry_price
        trade_lock[f"{symbol_timeframe}_entry_time"]       = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
        trade_lock[f"{symbol_timeframe}_entry_side"]       = 'long' if signal_side == 'buy' else 'short'
        trade_lock[f"{symbol_timeframe}_entry_brick_count"] = len(bricks)
        save_trade_lock(trade_lock)

        if telegram_config and telegram_config.get('bot_token') and telegram_config.get('chat_id'):
            msg = (
                f"ZEROBOT (EAR): {symbol} ({timeframe})\n"
                f"- Richtung: {pos_side.upper()}\n"
                f"- Entry: ${entry_price:.6f}\n"
                f"- SL: ${sl_rounded:.6f} (Brick-Open, {sl_dist/entry_price*100:.3f}%)\n"
                f"- TP: erster Gegenbrick (dynamisch)"
            )
            send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)

        logger.info("Trade-Eröffnung erfolgreich. TP via Brick-Reversal-Check.")

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
            # Position offen: SL liegt auf der Börse, TP via Brick-Reversal prüfen
            check_and_close_on_brick_reversal(exchange, pos[0], params, telegram_config, logger)
        else:
            housekeeper_routine(exchange, symbol, logger)
            check_and_open_new_position(exchange, model, scaler, params, telegram_config, logger)
    except Exception as e:
        logger.error(f"Fehler im Zyklus: {e}", exc_info=True)
        time.sleep(5)
