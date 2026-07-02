#!/usr/bin/env python3
"""
cleanup_trade_lock.py — Entfernt verwaiste Eintraege aus artifacts/db/trade_lock.json

Wenn eine Symbol/Timeframe-Kombination aus settings.json -> active_strategies entfernt
wird, ruft master_runner full_trade_cycle() dafuer nicht mehr auf. Damit laeuft auch
_notify_sl_fired() nie wieder, das normalerweise den *_position_open-Flag aufraeumt,
sobald eine Position auf der Exchange verschwindet. Solche Keys bleiben dann dauerhaft
(faelschlich) im trade_lock stehen, obwohl auf der Exchange laengst nichts mehr offen ist.

Aufruf:
  python3 cleanup_trade_lock.py            # zeigt an, was entfernt wuerde (dry-run)
  python3 cleanup_trade_lock.py --apply    # entfernt tatsaechlich und schreibt die Datei
"""
import os
import sys
import json
import argparse

PROJECT_ROOT     = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH    = os.path.join(PROJECT_ROOT, 'settings.json')
TRADE_LOCK_PATH  = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'trade_lock.json')


def load_active_symbol_timeframes():
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    return {
        f"{s['symbol'].replace('/', '-')}_{s['timeframe']}"
        for s in strategies if s.get('active')
    }


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

    keep, drop = {}, {}
    for key, value in trade_lock.items():
        is_active_key = any(key == prefix or key.startswith(prefix + '_') for prefix in active)
        if is_active_key:
            keep[key] = value
        else:
            drop[key] = value

    print(f"\nVerwaiste Keys (nicht mehr in active_strategies), {len(drop)} von {len(trade_lock)}:")
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
