# src/zerobot/utils/trade_manager.py
# Trade-Management für zerobot (Quantum State Signale)
#
# Unterschiede zu dnabot:
#   - Signal kommt von signal_logic (Quantum State + TE-Boost)
#   - Self-Learning: speichert auch Hurst + ApEn aus dem Tracker
#   - Konfidenz-Score wird in Telegram-Nachricht angezeigt
#   - BTC-Daten werden für Transfer Entropy parallel geladen

import logging
import time
import json
import os
import sys
import ccxt
import pandas as pd
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TRACKER_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'tracker')

sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.telegram import send_message
from zerobot.utils.exchange import Exchange
from zerobot.physics.database import StateDB
from zerobot.strategy.signal_logic import get_quantum_signal, update_state_with_trade_result

MIN_NOTIONAL_USDT = 5.0
FETCH_LIMIT = 300


# ─── Tracker ─────────────────────────────────────────────────────────────────

def get_tracker_file_path(symbol: str, timeframe: str) -> str:
    os.makedirs(TRACKER_DIR, exist_ok=True)
    safe = f"{symbol.replace('/', '-').replace(':', '-')}_{timeframe}.json"
    return os.path.join(TRACKER_DIR, safe)


def read_tracker(path: str) -> dict:
    default = {
        "status": "ok_to_trade",
        "last_side": None,
        "stop_loss_ids": [],
        "take_profit_ids": [],
        "active_state": None,
        "performance": {
            "total_trades": 0, "wins": 0, "losses": 0,
            "consecutive_losses": 0, "consecutive_wins": 0,
        }
    }
    if not os.path.exists(path):
        _write_tracker(path, default)
        return default
    try:
        with open(path, 'r') as f:
            content = f.read()
        return json.loads(content) if content else default
    except (json.JSONDecodeError, FileNotFoundError):
        _write_tracker(path, default)
        return default


def _write_tracker(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Fehler beim Schreiben des Trackers {path}: {e}")


# ─── Performance Tracking ─────────────────────────────────────────────────────

def record_trade_result(path: str, outcome: str, logger: logging.Logger):
    tracker = read_tracker(path)
    perf = tracker.setdefault('performance', {
        "total_trades": 0, "wins": 0, "losses": 0,
        "consecutive_losses": 0, "consecutive_wins": 0,
    })
    perf['total_trades'] = perf.get('total_trades', 0) + 1
    if outcome == 'win':
        perf['wins'] = perf.get('wins', 0) + 1
        perf['consecutive_wins'] = perf.get('consecutive_wins', 0) + 1
        perf['consecutive_losses'] = 0
    else:
        perf['losses'] = perf.get('losses', 0) + 1
        perf['consecutive_losses'] = perf.get('consecutive_losses', 0) + 1
        perf['consecutive_wins'] = 0

    total = perf['total_trades']
    if total > 0:
        perf['win_rate'] = perf['wins'] / total
    _write_tracker(path, tracker)


def should_skip_trading(path: str) -> tuple[bool, str]:
    tracker = read_tracker(path)
    perf = tracker.get('performance', {})
    if perf.get('consecutive_losses', 0) >= 5:
        return True, f"{perf['consecutive_losses']} aufeinanderfolgende Verluste"
    total = perf.get('total_trades', 0)
    if total >= 30 and perf.get('win_rate', 1.0) < 0.25:
        return True, f"Win-Rate {perf.get('win_rate', 0):.1%} nach {total} Trades"
    return False, "OK"


# ─── Order Management ─────────────────────────────────────────────────────────

def cancel_entry_orders(exchange: Exchange, symbol: str, logger: logging.Logger,
                         tracker_path: str = None):
    protected_ids: set = set()
    if tracker_path:
        try:
            t = read_tracker(tracker_path)
            protected_ids.update(t.get('take_profit_ids', []))
            protected_ids.update(t.get('stop_loss_ids', []))
        except Exception:
            pass

    for order in exchange.fetch_open_orders(symbol):
        if order['id'] in protected_ids:
            continue
        try:
            exchange.cancel_order(order['id'], symbol)
            time.sleep(0.1)
        except ccxt.OrderNotFound:
            pass
        except Exception as e:
            logger.warning(f"Konnte Order {order['id']} nicht stornieren: {e}")

    for order in exchange.fetch_open_trigger_orders(symbol):
        if order.get('reduceOnly') or order['id'] in protected_ids:
            continue
        try:
            exchange.cancel_trigger_order(order['id'], symbol)
            time.sleep(0.1)
        except ccxt.OrderNotFound:
            pass
        except Exception as e:
            logger.warning(f"Konnte Trigger {order['id']} nicht stornieren: {e}")


def ensure_tp_sl(exchange: Exchange, position: dict, signal: dict,
                  params: dict, tracker_path: str, logger: logging.Logger):
    symbol = params['market']['symbol']
    pos_side = position['side']

    triggers = exchange.fetch_open_trigger_orders(symbol)
    trigger_ids = {o['id'] for o in triggers}

    tracker = read_tracker(tracker_path)
    tp_ids = set(tracker.get('take_profit_ids', []))
    sl_ids = set(tracker.get('stop_loss_ids', []))

    tp_exists = bool(tp_ids)
    if sl_ids:
        sl_exists = bool(sl_ids & trigger_ids)
    else:
        entry_price = float(position.get('entryPrice', 0))
        sl_exists = any(
            o.get('reduceOnly') and (
                (pos_side == 'long' and o.get('side') == 'sell' and float(o.get('triggerPrice', 0)) < entry_price) or
                (pos_side == 'short' and o.get('side') == 'buy' and float(o.get('triggerPrice', 0)) > entry_price)
            )
            for o in triggers
        )

    if tp_exists and sl_exists:
        return

    logger.warning(f"Trailing Stop={tp_exists}, SL={sl_exists} fehlen — nachtragen...")

    contracts = float(position.get('contracts', 0))
    if contracts == 0:
        return

    active_state = tracker.get('active_state') or {}
    tp_price = (signal.get('tp_price') if signal else None) or active_state.get('tp_price')
    sl_price = (signal.get('sl_price') if signal else None) or active_state.get('sl_price')
    if not tp_price or not sl_price:
        logger.warning("Kein tp_price/sl_price verfügbar — Nachtragen nicht möglich.")
        return

    trailing_callback = params['risk'].get('trailing_callback_rate_pct', 1.0) / 100.0
    new_tp_ids = list(tp_ids)
    new_sl_ids = list(sl_ids)

    try:
        if not tp_exists:
            trail_side = 'sell' if pos_side == 'long' else 'buy'
            o = exchange.place_trailing_stop_order(symbol, trail_side, contracts, tp_price, trailing_callback)
            if o and 'id' in o:
                new_tp_ids = [o['id']]
            time.sleep(0.2)

        if not sl_exists:
            sl_side = 'sell' if pos_side == 'long' else 'buy'
            o = exchange.place_trigger_market_order(symbol, sl_side, contracts, sl_price, reduce=True)
            if o and 'id' in o:
                new_sl_ids = [o['id']]
    except Exception as e:
        logger.error(f"Fehler beim Nachtragen: {e}", exc_info=True)

    tracker['take_profit_ids'] = new_tp_ids
    tracker['stop_loss_ids'] = new_sl_ids
    _write_tracker(tracker_path, tracker)


def housekeeper_routine(exchange: Exchange, symbol: str, logger: logging.Logger) -> bool:
    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(1)
        position = exchange.fetch_open_positions(symbol)
        if position:
            pos_info = position[0]
            close_side = 'sell' if pos_info['side'] == 'long' else 'buy'
            logger.warning(f"Housekeeper: Verwaiste Position ({pos_info['side']}) — schließe...")
            exchange.place_market_order(symbol, close_side, float(pos_info['contracts']), reduce=True)
            time.sleep(3)
        return True
    except Exception as e:
        logger.error(f"Housekeeper-Fehler: {e}", exc_info=True)
        return False


# ─── Entry Orders ─────────────────────────────────────────────────────────────

def place_entry_orders(
    exchange: Exchange,
    signal: dict,
    params: dict,
    balance: float,
    tracker_path: str,
    telegram_config: dict,
    logger: logging.Logger,
):
    symbol = params['market']['symbol']
    side = signal.get('side')

    if side is None:
        return

    if side == 'long' and not params.get('behavior', {}).get('use_longs', True):
        logger.info("Longs deaktiviert.")
        return
    if side == 'short' and not params.get('behavior', {}).get('use_shorts', True):
        logger.info("Shorts deaktiviert.")
        return

    skip, reason = should_skip_trading(tracker_path)
    if skip:
        logger.warning(f"Trading pausiert: {reason}")
        return

    risk = params['risk']
    leverage = risk['leverage']
    risk_pct = risk.get('risk_per_entry_pct', 1.0)
    trailing_callback = risk.get('trailing_callback_rate_pct', 1.0) / 100.0

    entry_price = signal['entry_price']
    sl_price = signal['sl_price']
    tp_price = signal['tp_price']
    sl_pct = signal['sl_pct']

    if sl_pct <= 0:
        logger.warning("SL-Distanz = 0. Überspringe.")
        return

    sl_distance_price = abs(entry_price - sl_price)
    risk_amount_usd = balance * (risk_pct / 100.0)
    amount_coins = risk_amount_usd / sl_distance_price

    min_amount = exchange.fetch_min_amount_tradable(symbol)
    if amount_coins < min_amount:
        logger.warning(f"Menge {amount_coins:.6f} unter Minimum {min_amount:.6f}.")
        return

    notional = amount_coins * entry_price
    if notional < MIN_NOTIONAL_USDT:
        logger.warning(f"Notional {notional:.2f} USDT unter Minimum {MIN_NOTIONAL_USDT} USDT.")
        return

    try:
        exchange.set_margin_mode(symbol, risk.get('margin_mode', 'isolated'))
        time.sleep(0.3)
        exchange.set_leverage(symbol, leverage, risk.get('margin_mode', 'isolated'))
        time.sleep(0.3)
    except Exception as e:
        logger.warning(f"Margin/Leverage Fehler: {e}")

    order_side = 'buy' if side == 'long' else 'sell'
    tp_side = sl_side = 'sell' if side == 'long' else 'buy'

    logger.info(
        f"[Entry] {side.upper()} {amount_coins:.6f} {symbol} | "
        f"Market @ ~{entry_price:.4f} | SL={sl_price:.4f} ({sl_pct:.2f}%) | "
        f"TP={tp_price:.4f} | Score={signal['score']:.3f} | Konfidenz={signal.get('confidence', 0):.2f}"
    )

    new_tp_ids = []
    new_sl_ids = []

    try:
        tp_order = exchange.place_trailing_stop_order(symbol, tp_side, amount_coins, tp_price, trailing_callback)
        if tp_order and 'id' in tp_order:
            new_tp_ids.append(tp_order['id'])
        time.sleep(0.2)

        sl_order = exchange.place_trigger_market_order(symbol, sl_side, amount_coins, sl_price, reduce=True)
        if sl_order and 'id' in sl_order:
            new_sl_ids.append(sl_order['id'])
        time.sleep(0.2)

        exchange.place_market_order(symbol, order_side, amount_coins, reduce=False,
                                    margin_mode=risk.get('margin_mode', 'isolated'))
    except ccxt.InsufficientFunds as e:
        logger.error(f"Nicht genug Guthaben: {e}")
        cancel_entry_orders(exchange, symbol, logger)
        return
    except Exception as e:
        logger.error(f"Fehler beim Platzieren: {e}", exc_info=True)
        cancel_entry_orders(exchange, symbol, logger)
        return

    # Tracker aktualisieren
    tracker = read_tracker(tracker_path)
    tracker['stop_loss_ids'] = new_sl_ids
    tracker['take_profit_ids'] = new_tp_ids
    tracker['last_side'] = side
    tracker['status'] = 'ok_to_trade'
    tracker['last_notified_entry_price'] = entry_price
    tracker['last_notified_side'] = side
    tracker['active_state'] = {
        "state_id":          signal['state_id'],
        "sequence":          signal['sequence'],
        "direction":         side.upper(),
        "seq_length":        signal['seq_length'],
        "score":             signal['score'],
        "te_boost":          signal.get('te_boost', 1.0),
        "winrate":           signal['winrate'],
        "total_occurrences": signal['total_occurrences'],
        "entry_price":       entry_price,
        "sl_price":          sl_price,
        "tp_price":          tp_price,
        "hurst":             signal.get('hurst', 0.5),
        "apen":              signal.get('apen', 1.0),
        "confidence":        signal.get('confidence', 0.0),
    }
    _write_tracker(tracker_path, tracker)

    # Telegram
    try:
        timeframe = params['market']['timeframe']
        dir_emoji = "🟢" if side == 'long' else "🔴"
        sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
        tp_dist_pct = abs(tp_price - entry_price) / entry_price * 100
        rr = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0
        risk_usdt = balance * risk_pct / 100.0
        h = signal.get('hurst', 0.5)
        h_regime = 'Trend' if h > 0.55 else ('Reversion' if h < 0.45 else 'Neutral')
        msg = (
            f"⚛️ zerobot SIGNAL: {symbol} ({timeframe})\n"
            f"{'─' * 32}\n"
            f"{dir_emoji} Richtung:   {side.upper()}\n"
            f"💰 Entry:       ${entry_price:.6f}\n"
            f"🛑 SL:          ${sl_price:.6f} (-{sl_dist_pct:.2f}%)\n"
            f"🎯 Trailing ab: ${tp_price:.6f} (+{tp_dist_pct:.2f}%)\n"
            f"🔁 Callback:    {trailing_callback*100:.1f}%\n"
            f"📊 R:R:         1:{rr:.1f}\n"
            f"⚙️ Hebel:       {leverage}x\n"
            f"🛡️ Risiko:      {risk_pct:.1f}% ({risk_usdt:.2f} USDT)\n"
            f"📦 Kontrakte:   {amount_coins:.4f}\n"
            f"{'─' * 32}\n"
            f"🧬 State ID:    {signal['state_id'][:8]}...\n"
            f"📈 Score:       {signal['score']:.3f} (TE x{signal.get('te_boost', 1.0):.2f})\n"
            f"✅ Winrate:     {signal['winrate']:.1%} | n={signal['total_occurrences']}\n"
            f"🎲 Konfidenz:   {signal.get('confidence', 0):.2f}\n"
            f"📐 Hurst:       {h:.3f} ({h_regime})\n"
            f"🌊 ApEn:        {signal.get('apen', 1.0):.3f}\n"
            f"🔢 Sequenz: {signal['sequence']}"
        )
        send_message(telegram_config.get('bot_token'), telegram_config.get('chat_id'), msg)
    except Exception as e:
        logger.warning(f"Telegram fehlgeschlagen: {e}")


# ─── Self-Learning ────────────────────────────────────────────────────────────

def self_learn_from_closed_trade(
    tracker_path: str, db: StateDB, outcome: str,
    exit_price: float, logger: logging.Logger
):
    tracker = read_tracker(tracker_path)
    active_state = tracker.get('active_state')
    if not active_state:
        return

    entry_price = active_state.get('entry_price', 0)
    direction = active_state.get('direction', 'LONG')

    if entry_price > 0 and exit_price > 0:
        if direction == 'LONG':
            actual_move_pct = (exit_price - entry_price) / entry_price * 100
        else:
            actual_move_pct = (entry_price - exit_price) / entry_price * 100
    else:
        actual_move_pct = 0.0

    # Hurst-Regime aus gespeicherten Werten ableiten
    h = active_state.get('hurst', 0.5)
    if h > 0.55:
        regime = 'TREND'
    elif h < 0.45:
        regime = 'REVERTING'
    else:
        regime = 'NEUTRAL'

    update_state_with_trade_result(
        db=db,
        state_id=active_state['state_id'],
        sequence=active_state['sequence'],
        market=tracker.get('market', ''),
        timeframe=tracker.get('timeframe', ''),
        direction=direction,
        seq_length=active_state['seq_length'],
        outcome=outcome,
        actual_move_pct=actual_move_pct,
        regime=regime,
        hurst_value=active_state.get('hurst', 0.5),
        apen_value=active_state.get('apen', 1.0),
    )

    tracker['active_state'] = None
    _write_tracker(tracker_path, tracker)


# ─── Haupt-Trading-Zyklus ─────────────────────────────────────────────────────

def full_trade_cycle(
    exchange: Exchange,
    params: dict,
    telegram_config: dict,
    db_path: str,
    logger: logging.Logger,
):
    """
    Vollständiger Handelszyklus für zerobot:

    1. OHLCV-Daten laden (Ziel + optional BTC für TE)
    2. Quantum-Signal berechnen (State + Physik + TE)
    3. Entry-Orders stornieren
    4. Offene Position verwalten ODER Entry platzieren
    5. Self-Learning nach Trade-Abschluss
    """
    symbol = params['market']['symbol']
    timeframe = params['market']['timeframe']
    tracker_path = get_tracker_file_path(symbol, timeframe)

    # Markt im Tracker speichern
    tracker = read_tracker(tracker_path)
    tracker['market'] = symbol
    tracker['timeframe'] = timeframe
    _write_tracker(tracker_path, tracker)

    # 1. Daten laden
    logger.info(f"Lade {FETCH_LIMIT} Kerzen für {symbol} ({timeframe})...")
    df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=FETCH_LIMIT)
    if df is None or len(df) < 60:
        logger.error(f"Zu wenig Daten. Abbruch.")
        return

    # BTC-Daten für Transfer Entropy (nur wenn konfiguriert und nicht BTC selbst)
    btc_df = None
    te_ref = params.get('physics', {}).get('te_reference_symbol', 'BTC/USDT:USDT')
    if params.get('physics', {}).get('transfer_entropy_enabled', True) and symbol != te_ref:
        try:
            btc_df = exchange.fetch_recent_ohlcv(te_ref, timeframe, limit=200)
            logger.info(f"BTC-Referenzdaten geladen: {len(btc_df)} Kerzen")
        except Exception as e:
            logger.warning(f"BTC-Daten konnten nicht geladen werden: {e}")

    # 2. Quantum-Signal
    db = StateDB(db_path)
    signal = get_quantum_signal(df, params, db, btc_df)

    if signal:
        logger.info(
            f"Quantum Signal: {signal['side'].upper()} | "
            f"Score: {signal['score']:.3f} | Konfidenz: {signal.get('confidence', 0):.2f} | "
            f"Hurst: {signal.get('hurst', 0.5):.3f} | ApEn: {signal.get('apen', 1.0):.3f}"
        )
    else:
        logger.info("Kein Quantum-Signal.")

    current_price = float(df['close'].iloc[-1])

    # 3. Entry-Orders stornieren
    cancel_entry_orders(exchange, symbol, logger, tracker_path)

    # 4. Position prüfen
    open_positions = exchange.fetch_open_positions(symbol)

    if open_positions:
        position = open_positions[0]
        logger.info(f"Offene Position: {position.get('side')} @ {position.get('entryPrice')}")

        try:
            exchange.set_margin_mode(symbol, params['risk'].get('margin_mode', 'isolated'))
            exchange.set_leverage(symbol, params['risk']['leverage'], params['risk'].get('margin_mode', 'isolated'))
        except Exception:
            pass

        ensure_tp_sl(exchange, position, signal, params, tracker_path, logger)

    else:
        housekeeper_routine(exchange, symbol, logger)

        tracker = read_tracker(tracker_path)
        had_tp_ids = bool(tracker.get('take_profit_ids'))
        had_sl_ids = bool(tracker.get('stop_loss_ids'))

        if had_tp_ids or had_sl_ids:
            active_state = tracker.get('active_state') or {}
            entry_price = active_state.get('entry_price', 0)
            last_side = tracker.get('last_side', 'long')
            sl_price = active_state.get('sl_price', 0)

            fill_price = None
            outcome = None
            try:
                closed_orders = exchange.fetch_recent_closed_market_orders(symbol, limit=10)
                reduce_fills = [
                    o for o in closed_orders
                    if o.get('reduceOnly') and o.get('status') in ('closed', 'filled')
                    and float(o.get('filled', 0) or 0) > 0
                ]
                if reduce_fills:
                    latest = max(reduce_fills, key=lambda o: o.get('timestamp') or 0)
                    fill_price = float(latest.get('average') or latest.get('price') or 0)
                    logger.info(f"Trailing Stop-Ausführung: fill @ {fill_price:.6f}")
            except Exception as e:
                logger.error(f"Fehler beim Abrufen der Ausführung: {e}")

            if fill_price and fill_price > 0 and entry_price > 0:
                if last_side == 'long':
                    outcome = 'win' if fill_price > entry_price else 'loss'
                else:
                    outcome = 'win' if fill_price < entry_price else 'loss'
                reason = "Trailing Stop"
            elif entry_price > 0 and sl_price > 0:
                if last_side == 'long':
                    outcome = 'loss' if current_price <= sl_price * 1.005 else 'win'
                else:
                    outcome = 'loss' if current_price >= sl_price * 0.995 else 'win'
                reason = "Stop Loss" if outcome == 'loss' else "Trailing Stop"
                fill_price = fill_price or current_price
            else:
                logger.warning("Trade geschlossen — kein Self-Learning möglich.")

            if outcome:
                outcome_label = 'WIN' if outcome == 'win' else 'LOSS'
                record_trade_result(tracker_path, outcome, logger)
                try:
                    price_for_learning = fill_price if fill_price else current_price
                    self_learn_from_closed_trade(tracker_path, db, outcome_label, price_for_learning, logger)
                except Exception as e:
                    logger.error(f"Self-Learning Fehler: {e}")
                emoji = "✅" if outcome == 'win' else "🛑"
                try:
                    send_message(
                        telegram_config.get('bot_token'),
                        telegram_config.get('chat_id'),
                        f"{emoji} zerobot {reason}: {symbol} ({timeframe}) → {outcome_label}"
                    )
                except Exception:
                    pass

            tracker = read_tracker(tracker_path)
            tracker.update({"stop_loss_ids": [], "take_profit_ids": [], "status": "ok_to_trade"})
            tracker.pop('last_notified_entry_price', None)
            tracker.pop('last_notified_side', None)
            _write_tracker(tracker_path, tracker)

        balance = exchange.fetch_balance_usdt()
        logger.info(f"Guthaben: {balance:.2f} USDT")

        if balance < MIN_NOTIONAL_USDT:
            logger.warning(f"Guthaben zu niedrig ({balance:.2f} USDT).")
            db.close()
            return

        if signal is None:
            logger.info("Kein Signal → kein Entry.")
            db.close()
            return

        place_entry_orders(exchange, signal, params, balance, tracker_path, telegram_config, logger)

    db.close()
    logger.info(f"Trade-Zyklus abgeschlossen für {symbol} ({timeframe}).")
