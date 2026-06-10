#!/usr/bin/env python3
"""
init_brick_states.py — Brick-State-Initialisierung für ZeroBot Live-Bot

Liest alle Configs, baut EAR-Bricks mit vollständiger Historie (ab _meta.train_start)
und schreibt den letzten Brick-State in artifacts/db/ear_brick_state_{symbol}_{tf}.json.

Der Live-Bot (trade_manager.py) liest diese Dateien als Startpunkt — damit beginnt
er sofort mit dem korrekten Brick-Level statt aus 1000 Kerzen neu aufzubauen.

Aufruf:
  python3 init_brick_states.py              # alle aktiven Configs
  python3 init_brick_states.py --all        # alle Configs (auch inaktive)
  python3 init_brick_states.py --force      # überschreibt vorhandene States
"""
import os
import sys
import json
import argparse
from datetime import date

import ta
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
DB_DIR      = os.path.join(PROJECT_ROOT, 'artifacts', 'db')

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
NC = '\033[0m'


def _state_path(symbol: str, timeframe: str) -> str:
    safe = f"{symbol.replace('/', '-').replace(':', '-')}_{timeframe}"
    return os.path.join(DB_DIR, f'ear_brick_state_{safe}.json')


def _load_active_pairs() -> set | None:
    """Gibt aktive (symbol, timeframe)-Paare aus settings.json zurück, oder None = alle."""
    try:
        with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
            s = json.load(f)
        entries = s.get('live_trading_settings', {}).get('active_strategies', [])
        pairs = set()
        for e in entries:
            if not e.get('active', True):
                continue
            sym = e.get('symbol', '').strip()
            tf  = e.get('timeframe', '').strip()
            if sym and tf:
                pairs.add((sym, tf))
        return pairs if pairs else None
    except Exception:
        return None


def init_brick_states(all_configs: bool = False, force: bool = False) -> int:
    from zerobot.analysis.backtester import load_data
    from zerobot.strategy.ear_engine import EAREngine

    os.makedirs(DB_DIR, exist_ok=True)

    active_pairs = None if all_configs else _load_active_pairs()

    config_files = sorted([
        f for f in os.listdir(CONFIGS_DIR)
        if f.startswith('config_') and f.endswith('.json')
    ]) if os.path.isdir(CONFIGS_DIR) else []

    if not config_files:
        print(f'{R}Keine Configs in {CONFIGS_DIR}{NC}')
        return 1

    today     = date.today().strftime('%Y-%m-%d')
    processed = 0
    skipped   = 0
    errors    = 0

    print(f'\n{"─"*60}')
    print(f'  ZeroBot — Brick-State-Initialisierung')
    print(f'  Configs: {len(config_files)} | Modus: {"alle" if all_configs else "nur aktive"}')
    print(f'{"─"*60}\n')

    for fname in config_files:
        try:
            with open(os.path.join(CONFIGS_DIR, fname)) as f:
                config = json.load(f)

            symbol    = config.get('market', {}).get('symbol', '')
            timeframe = config.get('market', {}).get('timeframe', '')
            if not symbol or not timeframe:
                continue

            if active_pairs is not None and (symbol, timeframe) not in active_pairs:
                continue

            state_file = _state_path(symbol, timeframe)
            if not force and os.path.exists(state_file):
                print(f'  {Y}→ Skip{NC}  {symbol} / {timeframe}  (State vorhanden, --force zum Überschreiben)')
                skipped += 1
                continue

            # Daten ab _meta.train_start laden (volle Historie = korrekte Brick-Level)
            warmup = config.get('_meta', {}).get('train_start')
            if not warmup:
                print(f'  {Y}→ Skip{NC}  {symbol} / {timeframe}  (kein _meta.train_start in Config)')
                skipped += 1
                continue

            print(f'  Lade  {symbol} / {timeframe}  ab {warmup}...', end=' ', flush=True)
            data = load_data(symbol, timeframe, warmup, today)
            if data is None or data.empty or len(data) < 50:
                print(f'{R}keine Daten{NC}')
                errors += 1
                continue

            # ATR berechnen (wie im Backtester)
            atr_ind = ta.volatility.AverageTrueRange(
                high=data['high'], low=data['low'], close=data['close'], window=14)
            data['atr'] = atr_ind.average_true_range()
            data.dropna(subset=['atr'], inplace=True)

            # Bricks aus voller Historie bauen — kein init_lc/init_direction nötig
            strategy_params = config.get('strategy', {})
            engine  = EAREngine(settings=strategy_params)
            bricks  = engine._build_bricks(data)

            if not bricks:
                print(f'{R}keine Bricks{NC}')
                errors += 1
                continue

            last = bricks[-1]
            state = {'lc': last['close'], 'direction': last['direction']}
            with open(state_file, 'w') as f:
                json.dump(state, f)

            print(f'{G}✓{NC}  lc={last["close"]:.6f}  dir={last["direction"]}  '
                  f'({len(bricks)} Bricks aus {len(data)} Kerzen)')
            processed += 1

        except Exception as e:
            print(f'{R}Fehler bei {fname}: {e}{NC}')
            errors += 1

    print(f'\n{"─"*60}')
    print(f'  Ergebnis: {G}{processed} initialisiert{NC}  |  '
          f'{Y}{skipped} übersprungen{NC}  |  {R}{errors} Fehler{NC}')
    print(f'{"─"*60}\n')
    return 0 if errors == 0 else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ZeroBot Brick-State-Initialisierung')
    parser.add_argument('--all',   action='store_true', help='Alle Configs (auch inaktive)')
    parser.add_argument('--force', action='store_true', help='Vorhandene States überschreiben')
    args = parser.parse_args()
    sys.exit(init_brick_states(all_configs=args.all, force=args.force))
