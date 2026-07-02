# src/zerobot/utils/trade_manager.py
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import numpy as np
import pandas as pd
import ta
import math

from zerobot.strategy.ear_engine import EAREngine
from zerobot.strategy.ear_logic import get_ear_signal
from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message, send_photo
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


def _chart_state_path(symbol_timeframe: str) -> str:
    safe = symbol_timeframe.replace('/', '-').replace(':', '-')
    return os.path.join(DB_PATH, f'ear_chart_state_{safe}.json')


def load_chart_state(symbol_timeframe: str) -> dict:
    """Laedt den init_lc/direction, der beim letzten Bot-Lauf verwendet wurde."""
    path = _chart_state_path(symbol_timeframe)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_chart_state(symbol_timeframe: str, lc, direction):
    """Speichert den init_lc/direction VOR dem Brick-Build — fuer show_live_charts."""
    os.makedirs(DB_PATH, exist_ok=True)
    with open(_chart_state_path(symbol_timeframe), 'w') as f:
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


def _pnl_str(entry_price, exit_price, side):
    """Berechnet PnL-Prozent als formatierten String."""
    try:
        ep  = float(entry_price)
        xp  = float(exit_price)
        pct = ((xp - ep) / ep * 100) if side == 'long' else ((ep - xp) / ep * 100)
        return f"{pct:+.2f}%"
    except Exception:
        return "?"


def _generate_brick_png(bricks: list, symbol: str, timeframe: str,
                        entry_price: float = None, exit_price: float = None,
                        entry_side: str = None, sl_price: float = None,
                        n_bricks: int = 60) -> str:
    """
    Zeichnet die letzten n_bricks EAR-Bricks als Renko-Chart (matplotlib PNG).
    Bricks kommen direkt aus _build_bricks mit persistiertem State — identisch zum Live-Bot.
    Gibt den Pfad zur temporaeren PNG-Datei zurueck (muss nach dem Senden geloescht werden).
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return None

    if not bricks:
        return None

    display_bricks = bricks[-n_bricks:]
    n = len(display_bricks)

    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor('#0d1117')
    ax.set_facecolor('#0d1117')

    brick_width = 0.8
    for i, b in enumerate(display_bricks):
        is_up  = b['direction'] == 'up'
        prev_c = display_bricks[i - 1]['close'] if i > 0 else b['close']
        b_open = prev_c
        b_close = b['close']
        color  = '#26a69a' if is_up else '#ef5350'
        bottom = min(b_open, b_close)
        height = abs(b_close - b_open)
        if height == 0:
            height = abs(b_close) * 1e-5
        rect = mpatches.FancyBboxPatch(
            (i - brick_width / 2, bottom), brick_width, height,
            boxstyle="square,pad=0",
            linewidth=0.5, edgecolor='#1e2a3a', facecolor=color, zorder=2,
        )
        ax.add_patch(rect)

    all_prices = [b['close'] for b in display_bricks]
    y_min = min(all_prices)
    y_max = max(all_prices)
    margin = (y_max - y_min) * 0.15 or y_min * 0.01
    ax.set_xlim(-1, n)
    ax.set_ylim(y_min - margin, y_max + margin)

    # Entry-Linie
    if entry_price is not None:
        ax.axhline(entry_price, color='#ffd700', linewidth=1.2, linestyle='--', zorder=3,
                   label=f"Entry {entry_price:.6g}")
        ax.text(n - 0.5, entry_price, f"  Entry\n  {entry_price:.6g}",
                color='#ffd700', fontsize=7.5, va='center', ha='left')

    # SL-Linie
    if sl_price is not None:
        ax.axhline(sl_price, color='#ff4444', linewidth=1.0, linestyle='--', zorder=3)
        ax.text(n - 0.5, sl_price, f"  SL\n  {sl_price:.6g}",
                color='#ff4444', fontsize=7.5, va='center', ha='left')

    # Exit-Linie (TP oder SL)
    if exit_price is not None and exit_price != entry_price:
        is_win = (exit_price > entry_price and entry_side == 'long') or \
                 (exit_price < entry_price and entry_side == 'short')
        exit_color = '#26a69a' if is_win else '#ef5350'
        ax.axhline(exit_price, color=exit_color, linewidth=1.2, linestyle=':', zorder=3,
                   label=f"Exit {exit_price:.6g}")
        ax.text(n - 0.5, exit_price, f"  Exit\n  {exit_price:.6g}",
                color=exit_color, fontsize=7.5, va='center', ha='left')

    side_label = f"{'LONG' if entry_side == 'long' else 'SHORT'} | " if entry_side else ""
    ax.set_title(f"{symbol}  {timeframe}  |  {side_label}letzte {n} EAR-Bricks",
                 color='#e0e0e0', fontsize=11, pad=10)
    ax.tick_params(colors='#888888', labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor('#2a3a4a')
    ax.set_xticks([])
    ax.yaxis.tick_right()
    ax.grid(axis='y', color='#1e2a3a', linewidth=0.5, zorder=1)

    if entry_price or exit_price:
        ax.legend(facecolor='#1a2332', edgecolor='#2a3a4a',
                  labelcolor='#cccccc', fontsize=8, loc='upper left')

    plt.tight_layout()

    tmp_dir  = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    sym_safe = symbol.replace('/', '-').replace(':', '-')
    path     = os.path.join(tmp_dir, f'brick_snapshot_{sym_safe}_{timeframe}_{ts}.png')
    fig.savefig(path, dpi=130, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def _send_brick_chart(bricks, symbol, timeframe, entry_price, exit_price,
                      entry_side, telegram_config, logger, sl_price=None):
    """Generiert PNG und sendet es via Telegram. Loescht Temp-Datei danach."""
    if not telegram_config or not telegram_config.get('bot_token') or not telegram_config.get('chat_id'):
        return
    try:
        path = _generate_brick_png(bricks, symbol, timeframe, entry_price, exit_price,
                                   entry_side, sl_price=sl_price)
        if path and os.path.exists(path):
            send_photo(telegram_config['bot_token'], telegram_config['chat_id'], path)
            os.remove(path)
    except Exception as e:
        logger.warning(f"Brick-Chart konnte nicht gesendet werden: {e}")


def _close_position(exchange, symbol, pos_info, params, telegram_config, logger, reason='brick_reversal'):
    """Schließt eine offene Position per Market Order."""
    try:
        contracts  = float(pos_info.get('contracts', 0))
        pos_side   = pos_info.get('side', '').lower()

        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(1)

        if contracts <= 0:
            logger.warning(f"_close_position: contracts={contracts}, überspringe.")
            return

        exchange.flash_close_position(symbol)
        logger.info(f"Position geschlossen ({reason}): {pos_side.upper()} {contracts} {symbol}")

        # _position_open-Flag löschen (Bot hat selbst geschlossen, kein SL-Fire-Signal nötig)
        tf               = params['market']['timeframe']
        symbol_timeframe = f"{symbol.replace('/', '-')}_{tf}"
        trade_lock       = load_or_create_trade_lock()
        trade_lock.pop(f'{symbol_timeframe}_position_open', None)
        save_trade_lock(trade_lock)

        if telegram_config and telegram_config.get('bot_token') and telegram_config.get('chat_id'):
            try:
                ticker      = exchange.fetch_ticker(symbol)
                exit_price  = ticker['last']
                entry_price = trade_lock.get(f'{symbol_timeframe}_last_entry_price')
                pnl         = _pnl_str(entry_price, exit_price, pos_side) if entry_price else "?"
                msg = (
                    f"ZEROBOT (EAR) - Trade geschlossen (TP)\n"
                    f"- Symbol: {symbol} ({tf})\n"
                    f"- Seite: {pos_side.upper()}\n"
                    f"- Exit: {exit_price:.8f}\n"
                    f"- PnL: {pnl}\n"
                    f"- Grund: {reason}"
                )
                send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)
                # Brick-Chart senden (mit persistiertem State = identisch zum Live-Bot)
                strat_params = params.get('strategy', {})
                recent_data  = exchange.fetch_recent_ohlcv(symbol, tf, limit=1000)
                if not recent_data.empty:
                    atr_ind = ta.volatility.AverageTrueRange(
                        high=recent_data['high'], low=recent_data['low'],
                        close=recent_data['close'], window=14)
                    recent_data['atr'] = atr_ind.average_true_range()
                    recent_data.dropna(subset=['atr'], inplace=True)
                    engine      = EAREngine(settings=strat_params)
                    bricks = engine._build_bricks(recent_data)
                    _send_brick_chart(bricks, symbol, tf,
                                      float(entry_price) if entry_price else None,
                                      float(exit_price), pos_side,
                                      telegram_config, logger)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Fehler beim Schließen der Position: {e}", exc_info=True)


def check_and_close_on_brick_reversal(exchange, pos_info, params, telegram_config, logger):
    """
    Prüft ob seit Trade-Eröffnung ein EAR-Brick in Gegenrichtung entstanden ist.
    Falls ja → Position per Market Order schließen (Brick-TP-Exit).

    Bricks werden ab dem exakten Entry-Anker (init_lc=Entry-Preis, init_direction=
    Entry-Richtung) über die Kerzen SEIT Entry-Zeitpunkt neu aufgebaut. Ein rollierendes
    Fenster (z.B. limit=1000 ab "jetzt") ist hier bewusst NICHT geeignet: _build_bricks ist
    pfadabhängig vom ersten Kerzen-Close im Fenster, und dieser Anfang verschiebt sich bei
    jedem Aufruf mit dem Fenster mit – dadurch faltet sich die komplette Brick-Kette bei
    jedem Check anders (verifiziert an Live-Daten: bereits 5 Kerzen Versatz kehren die
    Richtung des jüngsten Bricks um). Ab einem festen Anker (Entry) bleibt die Kette dagegen
    über alle Checks hinweg identisch – neue Kerzen hängen sich nur an, nichts faltet neu.
    """
    symbol            = params['market']['symbol']
    timeframe         = params['market']['timeframe']
    symbol_timeframe  = f"{symbol.replace('/', '-')}_{timeframe}"
    pos_side          = pos_info.get('side', '').lower()  # 'long' or 'short'

    trade_lock     = load_or_create_trade_lock()
    entry_price    = trade_lock.get(f"{symbol_timeframe}_last_entry_price")
    entry_time_str = trade_lock.get(f"{symbol_timeframe}_entry_time")
    entry_side     = trade_lock.get(f"{symbol_timeframe}_entry_side")  # 'long' / 'short'

    if entry_price is None or entry_time_str is None or entry_side is None:
        logger.warning("Kein Entry-Anker (Preis/Zeit/Seite) im trade_lock – Reversal-Check übersprungen.")
        return

    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
        since_ms = int(entry_dt.timestamp() * 1000)

        recent_data = exchange.fetch_ohlcv_since(symbol, timeframe, since_ms)
        if recent_data.empty:
            logger.info("Noch keine neuen Kerzen seit Entry – Position hält.")
            return

        strat_params   = params.get('strategy', {})
        engine         = EAREngine(settings=strat_params)
        init_direction = 'up' if entry_side == 'long' else 'down'
        bricks = engine._build_bricks(recent_data, init_lc=float(entry_price), init_direction=init_direction)

        if not bricks:
            logger.info(f"Kein Gegenbrick seit Entry ({entry_time_str}) – Position hält ({pos_side}).")
            return

        for brick in bricks:
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

        # EAR Engine — immer ab Kerze 0 der 1000 Kerzen, kein init_lc (keine Phantom-Bricks)
        engine = EAREngine(settings=strat_params)

        processed_data = engine.process_dataframe(recent_data)
        current_candle = processed_data.iloc[-1]

        signal_side, signal_price = get_ear_signal(processed_data, current_candle, params, Bias.NEUTRAL)

        if not signal_side:
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
        bricks = engine._build_bricks(recent_data)
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
            hold_side = 'long'
        else:
            pos_side  = 'sell'
            tsl_side  = 'buy'
            hold_side = 'short'

        sl_rounded = float(exchange.exchange.price_to_precision(symbol, sl_price))

        logger.info(
            f"Eröffne {pos_side.upper()}-Position: {amount:.6f} @ ${entry_price:.6f} | "
            f"SL: ${sl_rounded:.6f} ({sl_dist/entry_price*100:.3f}%) | Risk: {risk_usdt:.2f} USDT"
        )

        entry_order = exchange.create_market_order(symbol, pos_side, amount, {'tradeSide': 'open'})

        if not entry_order:
            return

        time.sleep(2)
        position = exchange.fetch_open_positions(symbol)
        if not position:
            return

        pos_info  = position[0]
        contracts = float(pos_info['contracts'])

        sl_result = exchange.place_sl_trigger_order(symbol, tsl_side, contracts, sl_rounded, hold_side)
        sl_ok     = bool(sl_result) and sl_result.get('code') == '00000'

        if not sl_ok:
            logger.error(f"SL-Trigger-Order fehlgeschlagen ({sl_result}) — schließe Position sofort wieder, "
                         f"kein ungeschützter Trade.")
            exchange.flash_close_position(symbol)
            if telegram_config and telegram_config.get('bot_token') and telegram_config.get('chat_id'):
                send_message(
                    telegram_config['bot_token'], telegram_config['chat_id'],
                    f"ZEROBOT (EAR) - WARNUNG: SL-Order fehlgeschlagen!\n"
                    f"- Symbol: {symbol} ({timeframe})\n"
                    f"- Antwort: {sl_result}\n"
                    f"- Position wurde sofort wieder geschlossen (kein ungeschützter Trade)."
                )
            return

        # Entry-Zeit, Seite und Position-Flag speichern
        set_trade_lock(symbol_timeframe)
        trade_lock = load_or_create_trade_lock()
        trade_lock[f"{symbol_timeframe}_last_entry_price"]  = entry_price
        trade_lock[f"{symbol_timeframe}_entry_time"]        = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        trade_lock[f"{symbol_timeframe}_entry_side"]        = 'long' if signal_side == 'buy' else 'short'
        trade_lock[f"{symbol_timeframe}_position_open"]     = True
        save_trade_lock(trade_lock)

        entry_side_str = 'long' if signal_side == 'buy' else 'short'
        if telegram_config and telegram_config.get('bot_token') and telegram_config.get('chat_id'):
            msg = (
                f"ZEROBOT (EAR) - Trade eroeffnet\n"
                f"- Symbol: {symbol} ({timeframe})\n"
                f"- Richtung: {pos_side.upper()}\n"
                f"- Entry: {entry_price:.8f}\n"
                f"- SL: {sl_rounded:.8f} ({sl_dist/entry_price*100:.2f}%)\n"
                f"- TP: erster Gegenbrick (dynamisch)"
            )
            send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)
            # Brick-Chart senden — bricks bereits berechnet (mit persistiertem State)
            _send_brick_chart(bricks, symbol, timeframe,
                              float(entry_price), None, entry_side_str,
                              telegram_config, logger, sl_price=float(sl_price))

        logger.info("Trade-Eröffnung erfolgreich. TP via Brick-Reversal-Check.")

    except ccxt.InsufficientFunds as e:
        logger.error(f"InsufficientFunds: {e}")
    except Exception as e:
        logger.error(f"Unerwarteter Fehler: {e}", exc_info=True)
        housekeeper_routine(exchange, symbol, logger)


def full_trade_cycle(exchange, model, scaler, params, telegram_config, logger):
    symbol           = params['market']['symbol']
    timeframe        = params['market']['timeframe']
    symbol_timeframe = f"{symbol.replace('/', '-')}_{timeframe}"
    try:
        pos = exchange.fetch_open_positions(symbol)
        if pos:
            check_and_close_on_brick_reversal(exchange, pos[0], params, telegram_config, logger)
        else:
            # Prüfen ob Bitget-SL gefeuert hat (Position war offen, jetzt weg, kein Bot-Exit)
            trade_lock = load_or_create_trade_lock()
            if trade_lock.get(f'{symbol_timeframe}_position_open'):
                _notify_sl_fired(exchange, symbol, timeframe, symbol_timeframe,
                                 trade_lock, telegram_config, logger)

            housekeeper_routine(exchange, symbol, logger)
            check_and_open_new_position(exchange, model, scaler, params, telegram_config, logger)
    except Exception as e:
        logger.error(f"Fehler im Zyklus: {e}", exc_info=True)
        time.sleep(5)


def _notify_sl_fired(exchange, symbol, timeframe, symbol_timeframe, trade_lock, telegram_config, logger):
    """Erkennt Bitget-SL-Fire und sendet Telegram-Benachrichtigung."""
    entry_side  = trade_lock.get(f'{symbol_timeframe}_entry_side', '?')
    entry_price = trade_lock.get(f'{symbol_timeframe}_last_entry_price')

    try:
        ticker     = exchange.fetch_ticker(symbol)
        exit_price = ticker['last']
    except Exception:
        exit_price = None

    pnl = _pnl_str(entry_price, exit_price, entry_side) if (entry_price and exit_price) else "?"
    logger.warning(f"SL ausgeloest: {entry_side.upper()} {symbol} | PnL ~{pnl}")

    if telegram_config and telegram_config.get('bot_token') and telegram_config.get('chat_id'):
        price_str = f"{float(exit_price):.8f}" if exit_price else "unbekannt"
        msg = (
            f"ZEROBOT (EAR) - SL ausgeloest\n"
            f"- Symbol: {symbol} ({timeframe})\n"
            f"- Seite: {entry_side.upper()}\n"
            f"- SL-Exit: ~{price_str}\n"
            f"- PnL: ~{pnl}"
        )
        send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)

    trade_lock.pop(f'{symbol_timeframe}_position_open', None)
    save_trade_lock(trade_lock)
