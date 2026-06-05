# tests/test_workflow.py
import pytest
import os
import sys
import json
import logging
import time
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.utils.trade_manager import check_and_open_new_position, housekeeper_routine
from zerobot.utils.trade_manager import set_trade_lock, is_trade_locked
from zerobot.utils.timeframe_utils import determine_htf


@pytest.fixture(scope="module")
def test_setup():
    print("\n--- Starte ZeroBot Workflow-Test (Renko) ---")

    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        pytest.skip("secret.json nicht gefunden.")

    with open(secret_path, 'r') as f:
        secrets = json.load(f)

    if not secrets.get('zerobot') or not secrets['zerobot']:
        pytest.skip("Kein 'zerobot'-Account in secret.json.")

    test_account    = secrets['zerobot'][0]
    telegram_config = secrets.get('telegram', {})

    try:
        exchange = Exchange(test_account)
        if not exchange.markets:
            pytest.fail("Exchange konnte nicht initialisiert werden.")
    except Exception as e:
        pytest.fail(f"Exchange-Fehler: {e}")

    symbol    = 'PEPE/USDT:USDT'
    timeframe = '15m'
    htf       = determine_htf(timeframe)

    params = {
        'market':   {'symbol': symbol, 'timeframe': timeframe, 'htf': htf},
        'strategy': {
            'atr_multiplier':     1.0,
            'trend_min_bricks':   3,
            'reversal_bricks':    2,
            'vol_filter_enabled': False,
            'min_vol_ratio':      1.2,
        },
        'risk': {
            'margin_mode':                     'isolated',
            'risk_per_trade_pct':              15.0,
            'risk_reward_ratio':               2.0,
            'leverage':                        20,
            'trailing_stop_activation_rr':     1.5,
            'trailing_stop_callback_rate_pct': 0.5,
            'atr_multiplier_sl':               1.0,
            'min_sl_pct':                      4.0,
        },
        'behavior': {'use_longs': True, 'use_shorts': True},
    }

    test_logger = logging.getLogger("test-logger")
    test_logger.setLevel(logging.INFO)
    if not test_logger.handlers:
        test_logger.addHandler(logging.StreamHandler(sys.stdout))

    print(f"-> Initiales Aufräumen für {symbol}...")
    try:
        housekeeper_routine(exchange, symbol, test_logger)
        time.sleep(2)
        pos = exchange.fetch_open_positions(symbol)
        if pos:
            exchange.create_market_order(symbol, 'sell' if pos[0]['side'] == 'long' else 'buy',
                                         float(pos[0]['contracts']), {'reduceOnly': True})
            time.sleep(2)
        print("-> Ausgangszustand sauber.")
    except Exception as e:
        pytest.fail(f"Fehler beim Aufräumen: {e}")

    yield exchange, params, telegram_config, symbol, test_logger

    print("\n[Teardown] Räume auf...")
    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(2)
        position = exchange.fetch_open_positions(symbol)
        if position:
            exchange.create_market_order(
                symbol,
                'sell' if position[0]['side'] == 'long' else 'buy',
                float(position[0]['contracts']),
                {'reduceOnly': True})
            time.sleep(3)
        exchange.cancel_all_orders_for_symbol(symbol)
        print("-> Aufräumen abgeschlossen.")
    except Exception as e:
        print(f"FEHLER beim Aufräumen: {e}")


def test_full_zerobot_workflow_on_bitget(test_setup):
    exchange, params, telegram_config, symbol, logger = test_setup

    bal = exchange.fetch_balance_usdt()
    print(f"\n--- Verfügbares Guthaben: {bal:.4f} USDT ---")

    with patch('zerobot.utils.trade_manager.set_trade_lock'), \
         patch('zerobot.utils.trade_manager.is_trade_locked', return_value=False), \
         patch('zerobot.utils.trade_manager.get_renko_signal', return_value=('buy', None)):

        print("\n[Schritt 1/3] Simuliere Buy-Signal und prüfe Trade-Eröffnung...")
        check_and_open_new_position(exchange, None, None, params, telegram_config, logger)

    print("-> Warte 5s auf Order-Ausführung...")
    time.sleep(5)

    print("\n[Schritt 2/3] Überprüfe Position und Orders...")
    position = exchange.fetch_open_positions(symbol)

    if not position:
        pytest.fail(f"Position nicht eröffnet. Guthaben war: {bal:.2f} USDT")

    assert len(position) == 1
    pos_info = position[0]
    print(f"-> Position eröffnet: {pos_info['side'].upper()} {pos_info['contracts']} PEPE.")

    trigger_orders = exchange.fetch_open_trigger_orders(symbol)
    print(f"-> Trigger-Orders: {len(trigger_orders)}")

    print("\n[Schritt 3/3] Schließe Position...")
    exchange.cancel_all_orders_for_symbol(symbol)
    time.sleep(3)

    amount_to_close = abs(float(pos_info.get('contracts', 0)))
    side_to_close   = 'sell' if pos_info.get('side', '').lower() == 'long' else 'buy'

    if amount_to_close > 0:
        close_order = exchange.create_market_order(
            symbol, side_to_close, amount_to_close, params={'reduceOnly': True})
        assert close_order, "Konnte Position nicht schließen!"
        print(f"-> Position geschlossen.")
        time.sleep(4)

    exchange.cancel_all_orders_for_symbol(symbol)
    time.sleep(2)

    final_positions = exchange.fetch_open_positions(symbol)
    final_orders    = exchange.fetch_open_trigger_orders(symbol)

    if len(final_orders) > 0:
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(2)
        final_orders = exchange.fetch_open_trigger_orders(symbol)

    assert len(final_positions) == 0, "Position sollte geschlossen sein."
    assert len(final_orders)    == 0, f"Trigger-Orders nicht gelöscht ({len(final_orders)} verbleibend)."

    print("\n--- WORKFLOW-TEST ERFOLGREICH! ---")
