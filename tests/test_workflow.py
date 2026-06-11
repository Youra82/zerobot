# tests/test_workflow.py
import pytest
import os
import sys
import json
import logging
import time
from unittest.mock import patch

# Zeige alle INFO-Logs aus dem Exchange-Modul im Test-Output
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format='[%(name)s] %(message)s')

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message
from zerobot.utils.trade_manager import check_and_open_new_position, housekeeper_routine
from zerobot.utils.trade_manager import set_trade_lock, save_trade_lock, is_trade_locked, load_or_create_trade_lock


@pytest.fixture(scope="module")
def test_setup():
    print("\n--- Starte ZeroBot Workflow-Test (EAR / PEPE) ---")

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

    # PEPE: kleine Mindestgröße (wie dnabot/mbot)
    symbol    = 'PEPE/USDT:USDT'
    timeframe = '15m'

    params = {
        'market': {'symbol': symbol, 'timeframe': timeframe, 'htf': None},
        'strategy': {
            'base_pct':         0.004,
            'k_entropy':        0.8,
            'h_window':         10,
            'trend_min_bricks': 3,
        },
        'risk': {
            'margin_mode':        'isolated',
            'risk_per_trade_pct': 0.1,   # SEHR KLEINES Risiko — wie dnabot (0.1%)
            'leverage':           20,
        },
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
            exchange.flash_close_position(symbol)
            time.sleep(2)
        print("-> Ausgangszustand sauber.")
    except Exception as e:
        pytest.fail(f"Fehler beim Aufräumen: {e}")

    yield exchange, params, telegram_config, symbol, test_logger

    print("\n[Teardown] Räume auf...")
    try:
        exchange.cancel_all_orders_for_symbol(symbol)
        time.sleep(1)
        position = exchange.fetch_open_positions(symbol)
        if position:
            exchange.flash_close_position(symbol)
            time.sleep(3)
        exchange.cancel_all_orders_for_symbol(symbol)
        print("-> Aufräumen abgeschlossen.")
    except Exception as e:
        print(f"FEHLER beim Aufräumen: {e}")


def test_full_zerobot_workflow_on_bitget(test_setup):
    exchange, params, telegram_config, symbol, logger = test_setup

    bal = exchange.fetch_balance_usdt()
    print(f"\n--- Verfügbares Guthaben: {bal:.4f} USDT ---")

    if bal < 5.0:
        pytest.skip(f"Zu wenig Guthaben ({bal:.2f} USDT < 5 USDT) für Live-Test.")

    # --- Schritt 1: Direkter Entry (kein EAR-Mock) ---
    # So vermeiden wir stale Brick-Level die über dem echten Marktpreis liegen.
    print(f"\n[Schritt 1/3] Öffne LONG direkt (no EAR) @ Marktpreis...")
    exchange.set_margin_mode(symbol, 'isolated')
    exchange.set_leverage(symbol, 20)

    ticker       = exchange.fetch_ticker(symbol)
    market_price = ticker['last']
    notional     = 8.0
    raw_amount   = notional / market_price
    entry_order  = exchange.create_market_order(symbol, 'buy', raw_amount, {'tradeSide': 'open'})
    if not entry_order:
        pytest.fail("Entry-Order fehlgeschlagen.")

    send_message(telegram_config.get('bot_token', ''), telegram_config.get('chat_id', ''),
        f"ZEROBOT Test - Trade eroeffnet\n"
        f"- Symbol: {symbol}\n"
        f"- Seite: LONG\n"
        f"- Notional: ~{notional} USDT @ {market_price:.8f}")

    print("-> Warte 3s auf Ausführung...")
    time.sleep(3)

    # --- Schritt 2: Position + SL prüfen ---
    print("\n[Schritt 2/3] Überprüfe Position und SL-Order...")
    position = exchange.fetch_open_positions(symbol)
    if not position:
        pytest.fail("Position nicht eröffnet.")

    assert len(position) == 1
    pos_info     = position[0]
    actual_entry = float(pos_info.get('entryPrice', market_price))
    mark_price   = float(pos_info.get('markPrice',  actual_entry))
    contracts    = float(pos_info['contracts'])
    print(f"-> Position: {pos_info['side'].upper()} {contracts} PEPE "
          f"(hedged={pos_info.get('hedged')}, marginMode={pos_info.get('marginMode')})")
    print(f"   entryPrice={actual_entry:.10f}  markPrice={mark_price:.10f}")

    # SL 3% unter echtem Entry → garantiert unter Marktpreis, feuert nicht sofort
    sl_price = actual_entry * 0.97
    print(f"   Platziere SL (pos_loss / place-tpsl-order) @ {sl_price:.10f} (-3% von entry)...")
    sl_result = exchange.place_sl_trigger_order(symbol, 'sell', contracts, sl_price, 'long')
    if not sl_result:
        print("WARNUNG: place_sl_trigger_order hat None zurückgegeben.")

    send_message(telegram_config.get('bot_token', ''), telegram_config.get('chat_id', ''),
        f"ZEROBOT Test - SL platziert\n"
        f"- planType=pos_loss, posSide=long\n"
        f"- SL @ {sl_price:.8f} (-3%)")
    time.sleep(2)

    trigger_orders = exchange.fetch_open_trigger_orders(symbol)
    if len(trigger_orders) == 0:
        print("WARNUNG: Keine Trigger-Orders gefunden.")
    else:
        print(f"-> SL-Order sichtbar: {len(trigger_orders)} Trigger-Order(s)")
        for o in trigger_orders:
            raw = o.get('info', {})
            print(f"   side={o.get('side')}  triggerPrice={o.get('triggerPrice')}  "
                  f"planType={raw.get('planType')}  posSide={raw.get('posSide')}  "
                  f"tradeSide={raw.get('tradeSide')}")

    # --- Schritt 3: Schließen ---
    print("\n[Schritt 3/3] Schließe Position (Flash-Close)...")
    exchange.cancel_all_orders_for_symbol(symbol)
    close_result = exchange.flash_close_position(symbol)
    if close_result:
        print("-> Flash-Close ausgeführt.")
    else:
        print("-> Flash-Close fehlgeschlagen (Position möglicherweise durch SL bereits geschlossen).")
    time.sleep(5)

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

    send_message(telegram_config.get('bot_token', ''), telegram_config.get('chat_id', ''),
        f"ZEROBOT Test - Trade geschlossen\n"
        f"- Symbol: {symbol}\n"
        f"- Position und SL-Order sauber gecancelt")

    print("\n--- WORKFLOW-TEST ERFOLGREICH! ---")
