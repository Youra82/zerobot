#!/usr/bin/env python3
"""
cleanup_trade_lock.py — Entfernt verwaiste Eintraege aus artifacts/db/trade_lock.json

Wenn eine Symbol/Timeframe-Kombination aus settings.json -> active_strategies (oder im
Autopilot-Modus aus optimal_portfolio) entfernt wird, laesst master_runner sie trotzdem
weiterlaufen, SOLANGE trade_lock eine offene Position dafuer zeigt (_add_orphaned_open_
positions in master_runner.py) - genau deshalb duerfen Keys mit *_position_open=true
hier NIEMALS geloescht werden, auch wenn ihr Symbol/Timeframe nicht mehr "aktiv" ist.
Geloescht werden nur Keys, die weder aktiv noch mit einer offenen Position verknuepft sind
(typischerweise: Position ist laengst zu, aber das Symbol/Timeframe ist auch aus der
Konfiguration gefallen, bevor _notify_sl_fired den Flag aufraeumen konnte).

Aufruf:
  python3 cleanup_trade_lock.py            # zeigt an, was entfernt wuerde (dry-run)
  python3 cleanup_trade_lock.py --apply    # entfernt tatsaechlich und schreibt die Datei
"""
import os
import sys
import json
import argparse

PROJECT_ROOT              = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH             = os.path.join(PROJECT_ROOT, 'settings.json')
TRADE_LOCK_PATH           = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'trade_lock.json')
OPTIMIZATION_RESULTS_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'optimization_results.json')
CONFIGS_DIR               = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')


def load_active_symbol_timeframes():
    """Vereinigung aus active_strategies (manueller Modus) und optimal_portfolio
    (Autopilot-Modus) - unabhaengig vom aktuell eingestellten Modus, da eine Position
    unter dem jeweils anderen Modus eroeffnet worden sein kann."""
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    result = {
        f"{s['symbol'].replace('/', '-')}_{s['timeframe']}"
        for s in strategies if s.get('active')
    }

    if os.path.exists(OPTIMIZATION_RESULTS_PATH):
        try:
            with open(OPTIMIZATION_RESULTS_PATH) as f:
                portfolio = json.load(f).get('optimal_portfolio', [])
            for entry in portfolio:
                if isinstance(entry, dict) and entry.get('symbol') and entry.get('timeframe'):
                    result.add(f"{entry['symbol'].replace('/', '-')}_{entry['timeframe']}")
                elif isinstance(entry, str):
                    config_path = os.path.join(CONFIGS_DIR, entry)
                    if os.path.exists(config_path):
                        with open(config_path) as cf:
                            market = json.load(cf).get('market', {})
                        if market.get('symbol') and market.get('timeframe'):
                            result.add(f"{market['symbol'].replace('/', '-')}_{market['timeframe']}")
        except Exception:
            pass

    return result


def load_open_position_prefixes(trade_lock):
    """Symbol/Timeframe-Prefixe mit *_position_open=true - diese Keys bleiben
    unabhaengig vom aktiven Portfolio immer erhalten (siehe Modul-Docstring)."""
    prefixes = set()
    for key, value in trade_lock.items():
        if key.endswith('_position_open') and value:
            prefixes.add(key[:-len('_position_open')])
    return prefixes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Aenderung tatsaechlich schreiben (sonst dry-run)')
    args = parser.parse_args()

    if not os.path.exists(TRADE_LOCK_PATH):
        print(f"Keine trade_lock.json gefunden unter {TRADE_LOCK_PATH}")
        return

    active = load_active_symbol_timeframes()
    print(f"Aktive Symbol/Timeframe-Kombinationen ({len(active)}):")
    for a in sorted(active):
        print(f"  {a}")

    with open(TRADE_LOCK_PATH) as f:
        trade_lock = json.load(f)

    open_positions = load_open_position_prefixes(trade_lock)
    if open_positions:
        print(f"\nOffene Positionen laut trade_lock (bleiben in jedem Fall erhalten), {len(open_positions)}:")
        for p in sorted(open_positions):
            print(f"  {p}")

    protected = active | open_positions

    keep, drop = {}, {}
    for key, value in trade_lock.items():
        is_protected_key = any(key == prefix or key.startswith(prefix + '_') for prefix in protected)
        if is_protected_key:
            keep[key] = value
        else:
            drop[key] = value

    print(f"\nVerwaiste Keys (weder aktiv noch offene Position), {len(drop)} von {len(trade_lock)}:")
    for k, v in drop.items():
        print(f"  - {k}: {v}")

    if not args.apply:
        print("\nDry-Run — nichts geschrieben. Mit --apply tatsaechlich entfernen.")
        return

    with open(TRADE_LOCK_PATH, 'w') as f:
        json.dump(keep, f, indent=4)
    print(f"\n{len(drop)} verwaiste Keys entfernt. {len(keep)} Keys verbleiben in {TRADE_LOCK_PATH}.")


if __name__ == '__main__':
    main()
