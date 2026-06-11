#!/usr/bin/env python3
"""
show_live_charts.py — Sendet aktuelle EAR-Brick-Charts per Telegram.

Liest alle aktiven Strategien aus settings.json, baut Bricks mit dem
persistierten State (identisch zum Live-Bot) und schickt einen PNG-Chart
pro Strategie an Telegram.

Aufruf:
    python show_live_charts.py
    python show_live_charts.py --symbol BTC/USDT:USDT --timeframe 4h
"""
import argparse
import json
import logging
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

import ta

from zerobot.strategy.ear_engine import EAREngine
from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message, send_photo
from zerobot.utils.trade_manager import (
    _generate_brick_png,
    load_brick_state,
    load_or_create_trade_lock,
)

logging.basicConfig(level=logging.WARNING, format='[%(levelname)s] %(message)s')
logger = logging.getLogger('show_live_charts')

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
TMP_DIR     = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')

DEFAULT_STRATEGY_PARAMS = {
    'base_pct':         0.004,
    'k_entropy':        0.8,
    'h_window':         10,
    'trend_min_bricks': 3,
}


def _load_secrets():
    path = os.path.join(PROJECT_ROOT, 'secret.json')
    with open(path) as f:
        return json.load(f)


def _load_settings():
    path = os.path.join(PROJECT_ROOT, 'settings.json')
    with open(path) as f:
        return json.load(f)


def _load_config(symbol: str, timeframe: str) -> dict:
    """Laedt die Optimizer-Config fuer ein Symbol/Timeframe. Fallback: Default-Params."""
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    path = os.path.join(CONFIGS_DIR, f'config_{safe}.json')
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _open_position_info(symbol: str, timeframe: str, trade_lock: dict) -> tuple:
    """Gibt (entry_price, entry_side) zurueck wenn eine Position offen ist, sonst (None, None)."""
    sym_tf = f"{symbol.replace('/', '-')}_{timeframe}"
    if not trade_lock.get(f'{sym_tf}_position_open'):
        return None, None
    return trade_lock.get(f'{sym_tf}_last_entry_price'), trade_lock.get(f'{sym_tf}_entry_side')


def generate_and_send_chart(exchange: Exchange, symbol: str, timeframe: str,
                             telegram_config: dict, trade_lock: dict) -> bool:
    """
    Baut EAR-Bricks mit persistiertem State und sendet PNG-Chart per Telegram.
    Gibt True bei Erfolg zurueck.
    """
    config = _load_config(symbol, timeframe)
    strat_params = {**DEFAULT_STRATEGY_PARAMS, **config.get('strategy', {})}

    symbol_timeframe = f"{symbol.replace('/', '-')}_{timeframe}"
    entry_price, entry_side = _open_position_info(symbol, timeframe, trade_lock)

    print(f"  Lade OHLCV {symbol} ({timeframe})...")
    recent_data = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=500)
    if recent_data.empty or len(recent_data) < 20:
        print(f"  WARNUNG: Nicht genug Daten fuer {symbol} ({timeframe}).")
        return False

    atr_ind = ta.volatility.AverageTrueRange(
        high=recent_data['high'], low=recent_data['low'],
        close=recent_data['close'], window=14)
    recent_data['atr'] = atr_ind.average_true_range()
    recent_data.dropna(subset=['atr'], inplace=True)

    # Bricks mit persistiertem State — identisch zum Live-Bot
    engine      = EAREngine(settings=strat_params)
    brick_state = load_brick_state(symbol_timeframe)
    bricks      = engine._build_bricks(
        recent_data,
        init_lc=brick_state.get('lc'),
        init_direction=brick_state.get('direction'),
    )

    if not bricks:
        print(f"  WARNUNG: Keine Bricks fuer {symbol} ({timeframe}).")
        return False

    last_direction = bricks[-1]['direction'].upper()
    last_close     = bricks[-1]['close']
    n_bricks       = len(bricks)
    has_position   = entry_price is not None

    print(f"  {n_bricks} Bricks | letzter: {last_direction} @ {last_close:.6g}"
          + (f" | Position: {entry_side.upper()} @ {float(entry_price):.6g}" if has_position else ""))

    os.makedirs(TMP_DIR, exist_ok=True)
    png_path = _generate_brick_png(
        bricks, symbol, timeframe,
        entry_price=float(entry_price) if entry_price else None,
        exit_price=None,
        entry_side=entry_side,
    )

    if not png_path or not os.path.exists(png_path):
        print(f"  FEHLER: PNG konnte nicht erstellt werden.")
        return False

    # Caption fuer das Foto
    pos_info = f"POSITION: {entry_side.upper()} @ {float(entry_price):.6g}" if has_position else "Keine offene Position"
    caption  = (
        f"ZEROBOT | {symbol} ({timeframe})\n"
        f"Bricks: {n_bricks} | Letzter: {last_direction} @ {last_close:.6g}\n"
        f"{pos_info}"
    )

    send_photo(telegram_config['bot_token'], telegram_config['chat_id'], png_path, caption=caption)
    os.remove(png_path)
    print(f"  Chart gesendet.")
    return True


def main():
    parser = argparse.ArgumentParser(description='EAR Brick-Charts per Telegram senden')
    parser.add_argument('--symbol',    type=str, help='Nur dieses Symbol (z.B. BTC/USDT:USDT)')
    parser.add_argument('--timeframe', type=str, help='Nur dieses Timeframe (z.B. 4h)')
    args = parser.parse_args()

    secrets  = _load_secrets()
    settings = _load_settings()

    tg = secrets.get('telegram', {})
    if not tg.get('bot_token') or not tg.get('chat_id'):
        print("FEHLER: Kein Telegram-Token/Chat-ID in secret.json.")
        sys.exit(1)

    if not secrets.get('zerobot'):
        print("FEHLER: Kein 'zerobot'-Account in secret.json.")
        sys.exit(1)

    print("Initialisiere Exchange...")
    exchange = Exchange(secrets['zerobot'][0])
    if not exchange.markets:
        print("FEHLER: Exchange konnte nicht initialisiert werden.")
        sys.exit(1)

    active_strategies = settings['live_trading_settings']['active_strategies']
    trade_lock        = load_or_create_trade_lock()

    # Filter: --symbol / --timeframe Argumente
    if args.symbol or args.timeframe:
        active_strategies = [
            s for s in active_strategies
            if (not args.symbol    or s['symbol']    == args.symbol)
            and (not args.timeframe or s['timeframe'] == args.timeframe)
        ]

    targets = [s for s in active_strategies if s.get('active', False)]

    if not targets:
        print("Keine aktiven Strategien gefunden.")
        send_message(tg['bot_token'], tg['chat_id'],
                     "ZEROBOT Charts: Keine aktiven Strategien in settings.json")
        return

    print(f"\n{len(targets)} aktive Strategie(n) — sende Charts...\n")
    send_message(tg['bot_token'], tg['chat_id'],
                 f"ZEROBOT - Live Brick-Charts ({len(targets)} Strategie(n))")

    ok = 0
    for s in targets:
        symbol    = s['symbol']
        timeframe = s['timeframe']
        print(f"[{symbol} / {timeframe}]")
        try:
            success = generate_and_send_chart(exchange, symbol, timeframe, tg, trade_lock)
            if success:
                ok += 1
        except Exception as e:
            print(f"  FEHLER: {e}")
        time.sleep(1)

    print(f"\nFertig: {ok}/{len(targets)} Charts gesendet.")


if __name__ == '__main__':
    main()
