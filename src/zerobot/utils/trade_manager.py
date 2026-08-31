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
from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message, send_photo
from zerobot.utils.timeframe_utils import determine_htf

PROJECT_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
ARTIFACTS_PATH  = os.path.join(PROJECT_ROOT, 'artifacts')
DB_PATH         = os.path.join(ARTIFACTS_PATH, 'db')
TRADE_LOCK_FILE = os.path.join(DB_PATH, 'trade_lock.json')


RECENT_BRICKS_KEEP = 20  # generous margin over max(trend_min_bricks, sl_bricks_back)


def _brick_state_path(symbol_timeframe: str) -> str:
    safe = symbol_timeframe.replace('/', '-').replace(':', '-')
    return os.path.join(DB_PATH, f'ear_brick_state_{safe}.json')


def load_brick_state(symbol_timeframe: str) -> dict:
    """Laedt persistierten EAR-Brick-State (lc, direction, last_processed_ts,
    recent_bricks) oder leeres Dict wenn noch keiner existiert."""
    path = _brick_state_path(symbol_timeframe)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_brick_state(symbol_timeframe: str, state: dict):
    """Speichert kompletten Brick-State (lc, direction, last_processed_ts,
    recent_bricks) fuer den naechsten Lauf -- damit setzt die naechste
    Kerzen-Pruefung die durchgehende Kette fort statt sie neu aufzubauen."""
    os.makedirs(DB_PATH, exist_ok=True)
    with open(_brick_state_path(symbol_timeframe), 'w') as f:
        json.dump(state, f)


def _bootstrap_brick_chain(exchange, symbol, timeframe, strat_params, warmup_start, logger):
    """Einmaliger Aufbau der durchgehenden Brick-Kette aus voller Historie ab
    warmup_start (wie init_brick_states.py / wie backtester.py) -- Startpunkt
    fuer die anschliessende inkrementelle Fortsetzung. Wird automatisch
    aufgerufen wenn noch kein persistierter State existiert."""
    end_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    data = exchange.fetch_historical_ohlcv(symbol, timeframe, warmup_start, end_str)
    if data is None or data.empty or len(data) < 50:
        logger.error(f"Bootstrap: keine ausreichenden historischen Daten fuer {symbol} ({timeframe}).")
        return None

    engine = EAREngine(settings=strat_params)
    bricks = engine._build_bricks(data)
    if not bricks:
        logger.error(f"Bootstrap: keine Bricks aus Historie fuer {symbol} ({timeframe}).")
        return None

    last = bricks[-1]
    recent = [[b['direction'], b['close']] for b in bricks[-RECENT_BRICKS_KEEP:]]
    return {
        'lc': last['close'],
        'direction': last['direction'],
        'last_processed_ts': data.index[-1].isoformat(),
        'recent_bricks': recent,
    }


def _check_new_bricks_signal(recent_before, new_bricks, trend_min_bricks, sl_bricks_back):
    """Prueft JEDE neue Kerze dieses Batches der Reihe nach (nicht nur die
    letzte!) auf ein gueltiges Entry-Signal und gibt das ERSTE zurueck, das
    auftritt. Im Normalbetrieb (ein Cron-Check pro geschlossener Kerze,
    typisch bei 15-Min-Cron auf 1h-6h-Timeframes) enthaelt ein Batch ohnehin
    nur eine neue Kerze. Bei Nachhol-Batches mit mehreren neuen Kerzen (z.B.
    nach Downtime) entspricht "erstes Signal gewinnt" der Reihenfolge, in
    der die Signale live tatsaechlich aufgetreten waeren -- eine Pruefung,
    die nur die letzte Kerze des Batches ansieht, wuerde Signale auf
    frueheren Kerzen im selben Batch sonst still verlieren.

    Pro Kerze wird die sig_map-Overwrite-Semantik aus
    EAREngine.process_dataframe repliziert: bei mehreren Bricks auf
    derselben Kerze gewinnt der letzte der die Bedingung erfuellt, ein
    spaeterer erfolgloser Check loescht einen frueheren Erfolg auf
    derselben Kerze NICHT (kein Reset, nur `continue`).

    recent_before: bereits persistierte (direction, close)-Paare vor diesem
    Batch. new_bricks: (candle_idx, direction, close)-Tupel aus diesem
    Batch, aufsteigend nach candle_idx (Reihenfolge aus _build_bricks).
    Gibt (side, entry_price, sl_price) oder (None, None, None) zurueck."""
    combined = [tuple(x) for x in recent_before]
    i = 0
    n = len(new_bricks)
    while i < n:
        cidx = new_bricks[i][0]
        winning_pos = None
        winning_side = None
        while i < n and new_bricks[i][0] == cidx:
            _, direction, close = new_bricks[i]
            combined.append((direction, close))
            pos = len(combined) - 1
            i += 1
            if pos + 1 < trend_min_bricks:
                continue
            window_dirs = [combined[j][0] for j in range(pos - trend_min_bricks + 1, pos + 1)]
            if all(d == 'up' for d in window_dirs):
                winning_pos, winning_side = pos, 'long'
            elif all(d == 'down' for d in window_dirs):
                winning_pos, winning_side = pos, 'short'
            # sonst: vorherigen Gewinner auf dieser Kerze (falls vorhanden) unveraendert lassen

        if winning_pos is not None:
            entry_price = combined[winning_pos][1]
            sl_idx = winning_pos - sl_bricks_back
            if sl_idx >= 0:
                sl_price = combined[sl_idx][1]
                return winning_side, entry_price, sl_price, cidx
            # nicht genug Historie fuer SL -- diese Kerze uebergehen, naechste pruefen

    return None, None, None, None


def update_brick_chain(exchange, symbol, timeframe, strat_params, meta, logger):
    """Fuehrt die durchgehende, persistierte Brick-Kette um alle seit dem
    letzten Check neu geschlossenen Kerzen fort -- exakt wie der Backtester
    (eine einzige kontinuierliche Kette ab dem historischen Start), NICHT
    wie ein rollierendes Fenster ohne Anker (das bisherige, strukturell
    andere Verhalten -- siehe Kommentar in check_and_open_new_position).

    Gibt ein dict zurueck mit 'new_bricks' (Liste von (candle_idx, direction,
    close) aus diesem Batch), 'recent_before' (persistierter State vor
    diesem Batch, fuer die Signal-Pruefung), 'last_candle_idx' (Index der
    zuletzt verarbeiteten Kerze in new_bricks' Nummerierung) und
    'last_candle_close'. None wenn nichts Neues vorliegt oder ein Fehler
    auftrat. Persistiert den aktualisierten State bereits selbst."""
    symbol_timeframe = f"{symbol.replace('/', '-')}_{timeframe}"
    state = load_brick_state(symbol_timeframe)

    # 'lc'/'direction' reichen fuer den alten init_brick_states.py-Dateiformat
    # (nur Brick-Level+Richtung); last_processed_ts/recent_bricks sind fuer die
    # inkrementelle Fortsetzung zusaetzlich zwingend -- fehlen sie, neu bootstrapen
    # statt mit unvollstaendigem State weiterzumachen.
    required_keys = {'lc', 'direction', 'last_processed_ts', 'recent_bricks'}
    if not state or not required_keys.issubset(state.keys()):
        warmup_start = (meta or {}).get('train_start')
        if not warmup_start:
            logger.error(f"Kein persistierter Brick-State und kein _meta.train_start "
                        f"fuer Bootstrap ({symbol_timeframe}).")
            return None
        logger.info(f"Kein persistierter Brick-State fuer {symbol_timeframe} "
                    f"-- initialisiere aus Historie ab {warmup_start}...")
        state = _bootstrap_brick_chain(exchange, symbol, timeframe, strat_params, warmup_start, logger)
        if state is None:
            return None
        save_brick_state(symbol_timeframe, state)
        logger.info(f"Brick-State initialisiert: lc={state['lc']:.6f} dir={state['direction']}")

    last_ts = pd.Timestamp(state['last_processed_ts'])
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize('UTC')
    since_ms = int(last_ts.timestamp() * 1000) + 1

    h_window = int(strat_params.get('h_window', 10))
    buffer_n = h_window + 5

    buffer_data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=buffer_n)
    new_data    = exchange.fetch_ohlcv_since(symbol, timeframe, since_ms)
    if new_data.empty:
        return None  # keine neuen abgeschlossenen Kerzen seit dem letzten Check

    combined_window = pd.concat([buffer_data, new_data]).sort_index()
    combined_window = combined_window[~combined_window.index.duplicated(keep='last')]

    H_raw = EAREngine.candle_entropy_vectorized(
        combined_window['open'].values, combined_window['high'].values,
        combined_window['low'].values, combined_window['close'].values)
    H_roll_full = pd.Series(H_raw).rolling(h_window, min_periods=1).mean().values

    is_new  = combined_window.index > last_ts
    new_only = combined_window[is_new]
    if new_only.empty:
        return None
    H_roll_new = H_roll_full[is_new]

    engine = EAREngine(settings=strat_params)
    raw_bricks = engine._build_bricks(new_only, init_lc=state['lc'], init_direction=state['direction'],
                                       precomputed_H_roll=H_roll_new)
    new_bricks = [(b['candle_idx'], b['direction'], b['close']) for b in raw_bricks]

    recent_before = [tuple(x) for x in state.get('recent_bricks', [])]
    combined_recent = recent_before + [(d, c) for (_, d, c) in new_bricks]
    combined_recent = combined_recent[-RECENT_BRICKS_KEEP:]

    if raw_bricks:
        new_lc, new_direction = raw_bricks[-1]['close'], raw_bricks[-1]['direction']
    else:
        new_lc, new_direction = state['lc'], state['direction']

    new_state = {
        'lc': new_lc,
        'direction': new_direction,
        'last_processed_ts': new_only.index[-1].isoformat(),
        'recent_bricks': [list(x) for x in combined_recent],
    }
    save_brick_state(symbol_timeframe, new_state)

    return {
        'new_bricks': new_bricks,
        'recent_before': recent_before,
        'last_candle_idx': len(new_only) - 1,
        'last_candle_close': float(new_only['close'].iloc[-1]),
        'last_candle_time': new_only.index[-1],
        'new_candle_times': list(new_only.index),  # candle_idx (lokal im Batch) -> Zeitstempel
    }


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


def _compute_ema(exchange, symbol, timeframe, period=100):
    """Aktueller EMA(period)-Wert auf Kerzen-Close, rein zur Visualisierung
    auf dem Telegram-Chart (kein Einfluss auf die Handelslogik). Holt genug
    Kerzen fuer eine eingeschwungene EMA (5x Periode, min. 300)."""
    try:
        data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=max(period * 5, 300))
        if data is None or len(data) < period:
            return None
        ema = data['close'].ewm(span=period, adjust=False).mean()
        return float(ema.iloc[-1])
    except Exception:
        return None


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
                        n_bricks: int = 60, ema_value: float = None,
                        ema_period: int = 100) -> str:
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

    display_bricks = bricks[-n_bricks:] if n_bricks is not None else bricks
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
    if ema_value is not None:
        all_prices = all_prices + [ema_value]
    y_min = min(all_prices)
    y_max = max(all_prices)
    margin = (y_max - y_min) * 0.15 or y_min * 0.01
    ax.set_xlim(-1, n)
    ax.set_ylim(y_min - margin, y_max + margin)

    # EMA-Referenzlinie (nur zur Visualisierung, kein Einfluss auf die Handelslogik) --
    # aktueller EMA-Wert als flache Linie, da der Chart brick-indiziert (nicht zeit-
    # indiziert) ist und ein 100er-EMA sich ueber die kurze Zeitspanne eines einzelnen
    # Trades ohnehin kaum bewegt.
    if ema_value is not None:
        ax.axhline(ema_value, color='#a78bfa', linewidth=1.0, linestyle='-.', zorder=3,
                   label=f"EMA{ema_period} {ema_value:.6g}")
        ax.text(-0.9, ema_value, f"EMA{ema_period}\n{ema_value:.6g}  ",
                color='#a78bfa', fontsize=7.5, va='center', ha='right')

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

    side_label  = f"{'LONG' if entry_side == 'long' else 'SHORT'} | " if entry_side else ""
    bricks_desc = f"letzte {n} EAR-Bricks" if n_bricks is not None else f"alle {n} EAR-Bricks seit Entry"
    ax.set_title(f"{symbol}  {timeframe}  |  {side_label}{bricks_desc}",
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
                      entry_side, telegram_config, logger, sl_price=None, n_bricks=60,
                      ema_value=None, ema_period=100):
    """Generiert PNG und sendet es via Telegram. Loescht Temp-Datei danach.

    n_bricks=None zeigt alle Bricks (fuer Exit-Charts, die bereits ab Entry
    anchoren) statt nur die letzten n_bricks (Default, fuer den Entry-Chart).
    ema_value: optionaler EMA-Referenzwert, rein zur Visualisierung."""
    if not telegram_config or not telegram_config.get('bot_token') or not telegram_config.get('chat_id'):
        return
    try:
        path = _generate_brick_png(bricks, symbol, timeframe, entry_price, exit_price,
                                   entry_side, sl_price=sl_price, n_bricks=n_bricks,
                                   ema_value=ema_value, ema_period=ema_period)
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
                sl_price    = trade_lock.get(f'{symbol_timeframe}_sl_price')
                entry_time_str = trade_lock.get(f'{symbol_timeframe}_entry_time')
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
                # Brick-Chart mit demselben Anker (Entry-Preis/-Richtung ab Entry-Zeitpunkt)
                # aufbauen, der auch die Reversal-Entscheidung getroffen hat - sonst zeigt das
                # Bild eine unabhaengig neu gefaltete (und damit potenziell abweichende) Kette.
                if entry_price is not None and entry_time_str is not None:
                    strat_params   = params.get('strategy', {})
                    entry_dt       = datetime.fromisoformat(entry_time_str)
                    since_ms       = int(entry_dt.timestamp() * 1000)
                    recent_data    = exchange.fetch_ohlcv_since(symbol, tf, since_ms)
                    if not recent_data.empty:
                        engine         = EAREngine(settings=strat_params)
                        init_direction = 'up' if pos_side == 'long' else 'down'
                        bricks = engine._build_bricks(recent_data, init_lc=float(entry_price),
                                                      init_direction=init_direction)
                        ema_value = _compute_ema(exchange, symbol, tf)
                        _send_brick_chart(bricks, symbol, tf,
                                          float(entry_price), float(exit_price), pos_side,
                                          telegram_config, logger,
                                          sl_price=float(sl_price) if sl_price else None,
                                          n_bricks=None, ema_value=ema_value)
            except Exception as chart_err:
                logger.error(f"Chart-Erstellung (TP-Exit) fehlgeschlagen: {chart_err}", exc_info=True)
    except Exception as e:
        logger.error(f"Fehler beim Schließen der Position: {e}", exc_info=True)


def check_and_close_on_brick_reversal(exchange, pos_info, params, telegram_config, logger):
    """
    Prüft ob seit dem letzten Check ein EAR-Brick in Gegenrichtung entstanden ist.
    Falls ja → Position per Market Order schließen (Brick-TP-Exit).

    Nutzt die durchgehende, persistierte Brick-Kette (update_brick_chain) --
    dieselbe Kette, an der auch check_and_open_new_position weiterbaut, und
    strukturell identisch zur Kette, die backtester.py fuer die komplette
    Optimierungs-/Validierungs-Pipeline verwendet (eine einzige Kette ab dem
    historischen Start, nie neu verankert). Vorher wurde hier bei jedem Check
    eine EIGENE, am Entry-Preis neu verankerte Kette aufgebaut -- stabil ueber
    wiederholte Checks, aber ein anderer Startpunkt als die Backtest-Kette,
    und Renko-Bricks sind stark pfadabhaengig. Das erzeugte TP-Exits die vom
    Backtester nie geprueft wurden.
    """
    symbol            = params['market']['symbol']
    timeframe         = params['market']['timeframe']
    pos_side          = pos_info.get('side', '').lower()  # 'long' or 'short'

    try:
        strat_params = params.get('strategy', {})
        meta         = params.get('_meta', {})
        result = update_brick_chain(exchange, symbol, timeframe, strat_params, meta, logger)
        if result is None:
            logger.info("Noch keine neuen Kerzen seit letztem Check – Position hält.")
            return

        opposite = 'down' if pos_side == 'long' else 'up'
        for cidx, direction, close in result['new_bricks']:
            if direction == opposite:
                logger.info(f"Brick-Reversal: {direction.upper()}-Brick nach {pos_side.upper()}-Entry → schließe Position.")
                _close_position(exchange, symbol, pos_info, params, telegram_config, logger,
                                f'brick_reversal_{direction}')
                return

        logger.info(f"Kein Gegenbrick seit letztem Check – Position hält ({pos_side}).")

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

        strat_params = params.get('strategy', {})
        meta         = params.get('_meta', {})

        # Durchgehende, persistierte Brick-Kette fortsetzen (update_brick_chain) --
        # strukturell identisch zur Kette, die backtester.py (und damit die gesamte
        # Optuna-/Walk-Forward-/OOS-Pipeline) verwendet: EINE kontinuierliche Kette
        # ab dem historischen Start, nie neu verankert. Vorher wurde hier bei jedem
        # Cron-Lauf aus einem rollierenden 1000-Kerzen-Fenster OHNE Anker neu gebaut
        # ("immer ab Kerze 0 der 1000 Kerzen") -- instabil (bereits 5 Kerzen Versatz
        # kehren die Richtung des juengsten Bricks um, an Live-Daten verifiziert) und
        # strukturell nie das System, das der Backtester geprueft hat.
        result = update_brick_chain(exchange, symbol, timeframe, strat_params, meta, logger)
        if result is None:
            logger.info("Keine neuen abgeschlossenen Kerzen seit letztem Check – überspringe.")
            return

        sl_bricks_back   = strat_params.get('sl_bricks_back', 1)
        trend_min_bricks = strat_params.get('trend_min_bricks', 3)
        signal_side_str, entry_price, sl_price, _ = _check_new_bricks_signal(
            result['recent_before'], result['new_bricks'], trend_min_bricks, sl_bricks_back)

        if not signal_side_str:
            logger.info("Kein EAR-Signal – überspringe.")
            return
        signal_side  = 'buy' if signal_side_str == 'long' else 'sell'
        signal_price = entry_price

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

        # entry_price/sl_price bereits oben aus der durchgehenden Brick-Kette bestimmt
        # (wie im Backtester: bricks[bidx]['close'] / bricks[bidx-sl_bricks_back]['close']).
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

        # Realer Fill-Preis der Market-Order kann vom theoretischen Brick-Close
        # abweichen (Cronjob laeuft alle 15 Min, dazwischen bewegt sich der Preis).
        # Fuer trade_lock/Reversal-Erkennung (check_for_reversal ankert die
        # Brick-Rekonstruktion an diesem Preis) den echten Exchange-Fill-Preis
        # verwenden statt des vorab kalkulierten theoretischen Werts. SL-Level
        # und Positionsgroesse bleiben unveraendert (SL ist ein struktureller
        # Brick-Preis, keine Distanz-vom-Entry-Groesse; Order-Sizing muss vor
        # der Order feststehen).
        real_entry_price = pos_info.get('entryPrice')
        real_entry_price = float(real_entry_price) if real_entry_price else entry_price
        fill_dev_pct      = abs(real_entry_price - entry_price) / entry_price * 100
        if fill_dev_pct > 0.05:
            logger.info(f"Fill-Preis weicht vom theoretischen Signal-Preis ab: "
                        f"geplant ${entry_price:.6f} vs. real ${real_entry_price:.6f} "
                        f"({fill_dev_pct:.3f}%)")

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
        trade_lock[f"{symbol_timeframe}_last_entry_price"]  = real_entry_price
        trade_lock[f"{symbol_timeframe}_sl_price"]          = sl_rounded
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
                f"- Entry: {real_entry_price:.8f}"
                + (f" (Signal: {entry_price:.8f}, {fill_dev_pct:.2f}% Abweichung)" if fill_dev_pct > 0.05 else "") + "\n"
                f"- SL: {sl_rounded:.8f} ({sl_dist/entry_price*100:.2f}%)\n"
                f"- TP: erster Gegenbrick (dynamisch)"
            )
            send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)
            # Brick-Chart senden — aus der durchgehenden Kette (persistierter Teil + neue Bricks)
            chart_bricks = ([{'direction': d, 'close': c} for d, c in result['recent_before']]
                            + [{'direction': d, 'close': c} for _, d, c in result['new_bricks']])
            ema_value = _compute_ema(exchange, symbol, timeframe)
            _send_brick_chart(chart_bricks, symbol, timeframe,
                              float(real_entry_price), None, entry_side_str,
                              telegram_config, logger, sl_price=float(sl_price),
                              ema_value=ema_value)

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
                                 trade_lock, telegram_config, logger, params)

            housekeeper_routine(exchange, symbol, logger)
            check_and_open_new_position(exchange, model, scaler, params, telegram_config, logger)
    except Exception as e:
        logger.error(f"Fehler im Zyklus: {e}", exc_info=True)
        time.sleep(5)


def _notify_sl_fired(exchange, symbol, timeframe, symbol_timeframe, trade_lock, telegram_config, logger, params=None):
    """Erkennt Bitget-SL-Fire und sendet Telegram-Benachrichtigung (inkl. Brick-Chart, analog zum TP-Exit)."""
    entry_side     = trade_lock.get(f'{symbol_timeframe}_entry_side', '?')
    entry_price    = trade_lock.get(f'{symbol_timeframe}_last_entry_price')
    sl_price       = trade_lock.get(f'{symbol_timeframe}_sl_price')
    entry_time_str = trade_lock.get(f'{symbol_timeframe}_entry_time')

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

        if params is not None and entry_price is not None and entry_time_str is not None:
            try:
                strat_params   = params.get('strategy', {})
                entry_dt       = datetime.fromisoformat(entry_time_str)
                since_ms       = int(entry_dt.timestamp() * 1000)
                recent_data    = exchange.fetch_ohlcv_since(symbol, timeframe, since_ms)
                if not recent_data.empty:
                    engine         = EAREngine(settings=strat_params)
                    init_direction = 'up' if entry_side == 'long' else 'down'
                    bricks = engine._build_bricks(recent_data, init_lc=float(entry_price),
                                                  init_direction=init_direction)
                    ema_value = _compute_ema(exchange, symbol, timeframe)
                    _send_brick_chart(bricks, symbol, timeframe,
                                      float(entry_price),
                                      float(exit_price) if exit_price else None,
                                      entry_side if entry_side in ('long', 'short') else None,
                                      telegram_config, logger,
                                      sl_price=float(sl_price) if sl_price else None,
                                      n_bricks=None, ema_value=ema_value)
            except Exception as chart_err:
                logger.error(f"Chart-Erstellung (SL-Fired) fehlgeschlagen: {chart_err}", exc_info=True)
        elif params is None:
            logger.warning("Kein 'params' übergeben - SL-Fired-Chart übersprungen.")
        elif entry_price is None or entry_time_str is None:
            logger.warning(f"Kein Entry-Preis/-Zeit im trade_lock ({symbol_timeframe}) - "
                           f"SL-Fired-Chart übersprungen (vermutlich Position von vor diesem Fix).")

    trade_lock.pop(f'{symbol_timeframe}_position_open', None)
    save_trade_lock(trade_lock)
