# src/zerobot/utils/strategy_list.py
"""
Geteilte Hilfsfunktionen zur Bestimmung der aktiven Symbol/Timeframe-Liste --
eigenstaendiges Modul ohne Abhaengigkeit auf master_runner.py oder
check_brick_sync.py, damit beide es importieren koennen ohne einen
zirkulaeren Import zu erzeugen.
"""
import os
import json


def parse_symbol_timeframe(prefix):
    """'ADA-USDT:USDT_2h' -> ('ADA/USDT:USDT', '2h'). Timeframe-Werte enthalten kein '_'."""
    symbol_part, _, timeframe = prefix.rpartition('_')
    if not symbol_part or not timeframe:
        return None, None
    return symbol_part.replace('-', '/', 1), timeframe


def add_orphaned_open_positions(strategy_list, script_dir):
    """
    Ergaenzt strategy_list um Symbol/Timeframe-Kombinationen, die laut trade_lock.json
    noch eine offene Position halten, aber aus active_strategies/optimal_portfolio
    herausgefallen sind (z.B. durch woechentliche Autopilot-Neuoptimierung). Ohne das
    wuerde full_trade_cycle fuer diese Position nie wieder aufgerufen - sie liefe
    komplett unbeaufsichtigt weiter (kein Brick-Reversal-Check, keine SL-Fired-Meldung),
    bis sie zufaellig wieder ins Portfolio rotiert oder die fixe SL-Order feuert.
    """
    trade_lock_path = os.path.join(script_dir, 'artifacts', 'db', 'trade_lock.json')
    if not os.path.exists(trade_lock_path):
        return strategy_list

    try:
        with open(trade_lock_path) as f:
            trade_lock = json.load(f)
    except Exception:
        return strategy_list

    already_covered = set()
    for s in strategy_list:
        if isinstance(s, dict) and s.get('symbol') and s.get('timeframe'):
            already_covered.add((s['symbol'], s['timeframe']))

    for key, value in trade_lock.items():
        if not key.endswith('_position_open') or not value:
            continue
        prefix = key[:-len('_position_open')]
        symbol, timeframe = parse_symbol_timeframe(prefix)
        if not symbol or not timeframe:
            continue
        if (symbol, timeframe) in already_covered:
            continue
        print(f"  [!] Nicht mehr im Portfolio, aber offene Position -> bleibt ueberwacht: "
              f"{symbol} ({timeframe})")
        strategy_list.append({'symbol': symbol, 'timeframe': timeframe, 'active': True})
        already_covered.add((symbol, timeframe))

    return strategy_list
