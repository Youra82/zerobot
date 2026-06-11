# tests/test_sl_tp_simulation.py
"""
Simulation-Tests fuer SL- und TP-Verhalten:

test_sl_placed_and_auto_cancelled:
  Long eroeffnen, SL 3% unter Markt setzen. Pruefen ob SL-Order korrekt
  konfiguriert ist (planType=pos_loss, posSide=long, tradeSide=close).
  Nach flash_close muss der SL auto-gecancelt sein (Position weg = SL weg).

test_tp_brick_reversal_closes_position:
  Position eroeffnen, dann EAREngine._build_bricks mocken so dass ein
  Gegenbrick (DOWN) nach dem Entry-Brick erscheint.
  check_and_close_on_brick_reversal soll die Position schliessen.
"""
import json
import logging
import os
import sys
import time
from unittest.mock import patch

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message
from zerobot.utils.trade_manager import (
    check_and_close_on_brick_reversal,
    load_or_create_trade_lock,
    save_trade_lock,
)

SYMBOL    = 'PEPE/USDT:USDT'
TIMEFRAME = '15m'
NOTIONAL  = 8.0  # USDT


@pytest.fixture(scope='module')
def exchange_fixture():
    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        pytest.skip("secret.json nicht gefunden.")
    with open(secret_path) as f:
        secrets = json.load(f)
    if not secrets.get('zerobot'):
        pytest.skip("Kein 'zerobot'-Account in secret.json.")

    ex = Exchange(secrets['zerobot'][0])
    if not ex.markets:
        pytest.fail("Exchange konnte nicht initialisiert werden.")

    bal = ex.fetch_balance_usdt()
    if bal < 5.0:
        pytest.skip(f"Zu wenig Guthaben ({bal:.2f} USDT) fuer Simulation.")

    tg = secrets.get('telegram', {})
    _cleanup(ex)
    yield ex, tg
    _cleanup(ex)


def _cleanup(exchange):
    exchange.cancel_all_orders_for_symbol(SYMBOL)
    time.sleep(1)
    pos = exchange.fetch_open_positions(SYMBOL)
    if pos:
        exchange.flash_close_position(SYMBOL)
        time.sleep(3)


def _open_long(exchange) -> dict:
    """Oeffnet eine kleine Long-Position, gibt pos_info zurueck."""
    exchange.set_margin_mode(SYMBOL, 'isolated')
    exchange.set_leverage(SYMBOL, 20)
    ticker = exchange.fetch_ticker(SYMBOL)
    price  = ticker['last']
    amount = NOTIONAL / price
    order  = exchange.create_market_order(SYMBOL, 'buy', amount, {'tradeSide': 'open'})
    assert order, "Entry-Order fehlgeschlagen."
    time.sleep(3)
    positions = exchange.fetch_open_positions(SYMBOL)
    assert positions, "Position wurde nicht eroeffnet."
    return positions[0]


# ---------------------------------------------------------------------------
# Test 1: SL korrekt platziert + auto-gecancelt wenn Position schliesst
# ---------------------------------------------------------------------------
def test_sl_placed_and_auto_cancelled(exchange_fixture):
    """
    SL 3% unter Marktpreis setzen (bleibt aktiv, faeuert nicht sofort).
    Prueft: planType=pos_loss, posSide=long, tradeSide=close.
    Nach flash_close: Position weg, SL automatisch gecancelt.

    Hinweis: Bitget erzwingt triggerPrice < markPrice fuer Long-SL (pos_loss).
    Ein sofortiges Feuern durch triggerPrice > markPrice ist nicht moeglich.
    """
    exchange, tg = exchange_fixture
    _cleanup(exchange)

    pos_info    = _open_long(exchange)
    entry_price = float(pos_info['entryPrice'])
    contracts   = float(pos_info['contracts'])

    # SL 3% unter Entry -> bleibt aktiv
    sl_price = entry_price * 0.97
    print(f"\n  Entry:  {entry_price:.10f}")
    print(f"  SL (-3%): {sl_price:.10f}")

    send_message(tg.get('bot_token', ''), tg.get('chat_id', ''),
        f"ZEROBOT Test - SL-Simulation\n"
        f"- Symbol: {SYMBOL}\n"
        f"- Entry: {entry_price:.8f}\n"
        f"- SL (-3%): {sl_price:.8f}")

    result = exchange.place_sl_trigger_order(SYMBOL, 'sell', contracts, sl_price, 'long')
    assert result and result.get('code') == '00000', \
        f"SL-Order fehlgeschlagen: {result}"

    time.sleep(2)

    # SL-Order pruefen
    trigger_orders = exchange.fetch_open_trigger_orders(SYMBOL)
    assert len(trigger_orders) == 1, \
        f"Erwartet 1 SL-Trigger-Order, gefunden: {len(trigger_orders)}"

    raw = trigger_orders[0].get('info', {})
    print(f"  SL-Order: planType={raw.get('planType')} posSide={raw.get('posSide')} "
          f"tradeSide={raw.get('tradeSide')} trigger={raw.get('triggerPrice')}")

    assert raw.get('planType') == 'pos_loss', \
        f"planType sollte 'pos_loss' sein, ist: {raw.get('planType')}"
    assert raw.get('posSide') == 'long', \
        f"posSide sollte 'long' sein, ist: {raw.get('posSide')}"
    assert raw.get('tradeSide') == 'close', \
        f"tradeSide sollte 'close' sein, ist: {raw.get('tradeSide')}"

    print("  SL-Eigenschaften korrekt. Schliesse Position per flash_close...")

    # Position schliessen -> SL wird auto-gecancelt
    exchange.flash_close_position(SYMBOL)
    time.sleep(5)

    positions      = exchange.fetch_open_positions(SYMBOL)
    trigger_orders = exchange.fetch_open_trigger_orders(SYMBOL)

    assert len(positions) == 0, "Position sollte geschlossen sein."
    assert len(trigger_orders) == 0, \
        f"SL-Order sollte nach Position-Close auto-gecancelt sein ({len(trigger_orders)} verbleibend)."

    send_message(tg.get('bot_token', ''), tg.get('chat_id', ''),
        f"ZEROBOT Test - SL OK\n"
        f"- planType=pos_loss, posSide=long, tradeSide=close\n"
        f"- Position geschlossen, SL auto-gecancelt")

    print("  Position geschlossen, SL auto-gecancelt. Test bestanden.")


# ---------------------------------------------------------------------------
# Test 2: TP via Brick-Reversal (EAR-Mock)
# ---------------------------------------------------------------------------
def test_tp_brick_reversal_closes_position(exchange_fixture):
    """
    EAREngine._build_bricks wird gemockt: nach Entry-Brick erscheint ein
    DOWN-Brick. check_and_close_on_brick_reversal soll die Position schliessen.
    """
    exchange, tg = exchange_fixture
    _cleanup(exchange)

    pos_info    = _open_long(exchange)
    entry_price = float(pos_info['entryPrice'])

    # Mock: 2 UP-Bricks (vor Entry) + 1 DOWN-Brick (Reversal nach Entry)
    mock_bricks = [
        {'direction': 'up',   'close': entry_price * 0.98},
        {'direction': 'up',   'close': entry_price * 0.99},
        {'direction': 'down', 'close': entry_price * 1.01},  # Gegenbrick
    ]

    # entry_brick_count=2 -> post_entry_bricks = bricks[2:] = [DOWN-Brick]
    sym_tf = f"{SYMBOL.replace('/', '-')}_{TIMEFRAME}"
    trade_lock = load_or_create_trade_lock()
    trade_lock[f'{sym_tf}_entry_brick_count'] = 2
    trade_lock[f'{sym_tf}_entry_side']        = 'long'
    save_trade_lock(trade_lock)

    params = {
        'market':   {'symbol': SYMBOL, 'timeframe': TIMEFRAME, 'htf': None},
        'strategy': {'base_pct': 0.004, 'k_entropy': 0.8, 'h_window': 10, 'trend_min_bricks': 3},
        'risk':     {'margin_mode': 'isolated', 'risk_per_trade_pct': 0.1, 'leverage': 20},
    }
    logger = logging.getLogger('test-tp')

    print(f"\n  Entry: {entry_price:.10f}")
    print(f"  Mock DOWN-Brick @ {entry_price * 1.01:.10f} -> Reversal erkannt")

    send_message(tg.get('bot_token', ''), tg.get('chat_id', ''),
        f"ZEROBOT Test - TP-Simulation (Brick-Reversal)\n"
        f"- Symbol: {SYMBOL}\n"
        f"- Entry: {entry_price:.8f}\n"
        f"- Mock DOWN-Brick: Reversal wird ausgeloest")

    with patch('zerobot.strategy.ear_engine.EAREngine._build_bricks', return_value=mock_bricks):
        check_and_close_on_brick_reversal(exchange, pos_info, params, tg, logger)

    time.sleep(3)

    positions = exchange.fetch_open_positions(SYMBOL)
    if positions:
        exchange.flash_close_position(SYMBOL)
        time.sleep(3)
        pytest.fail("Position sollte durch Brick-Reversal (TP) geschlossen sein, ist aber noch offen.")

    send_message(tg.get('bot_token', ''), tg.get('chat_id', ''),
        f"ZEROBOT Test - TP OK\n"
        f"- Brick-Reversal erkannt, Position geschlossen")

    print("  TP via Brick-Reversal — Position erfolgreich geschlossen.")
