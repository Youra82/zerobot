#!/usr/bin/env python3
# scan_and_learn.py
# Quantum Pattern Discovery Pipeline — Lernprozess des zerobot
#
# Was passiert:
#   1. Für jedes Symbol + Timeframe: historische OHLCV-Daten laden
#   2. Kerzen zu Quantum States kodieren (Dir + Body + Wick + Hurst + Entropy + Vol)
#   3. Sliding-Window: Quantum-State-Muster entdecken → SQLite-DB
#   4. Evolver: States physics-informiert bewerten, aktivieren/deaktivieren
#   5. Report ausgeben
#
# Ausführung:
#   .venv/bin/python3 scan_and_learn.py
#   .venv/bin/python3 scan_and_learn.py --symbol BTC/USDT:USDT --timeframe 4h

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.physics.database import StateDB
from zerobot.physics.discovery import discover_states
from zerobot.physics.evolver import evolve, print_state_report
from zerobot.physics.hurst import get_current_hurst

os.makedirs(os.path.join(PROJECT_ROOT, 'logs'), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(PROJECT_ROOT, 'logs', 'scan_and_learn.log'), mode='a'
        ),
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'quantum.db')

# ── Automatische Defaults pro Timeframe ──────────────────────────────────────
# Kürzere Sequenzen (3-5) brauchen weniger History als dnabot (4-6)

HISTORY_DAYS_MAP = {
    '5m':  90,
    '15m': 120,
    '30m': 180,
    '1h':  365,
    '2h':  365,
    '4h':  730,
    '6h':  730,
    '8h':  1095,
    '12h': 1095,
    '1d':  1095,
}

DISCOVERY_HORIZON_MAP = {
    '5m':  288,   # 1 Tag
    '15m': 96,
    '30m': 48,
    '1h':  24,
    '2h':  12,
    '4h':  6,
    '6h':  4,
    '8h':  3,
    '12h': 2,
    '1d':  3,
}

MOVE_THRESHOLD_MAP = {
    '5m':  0.15,
    '15m': 0.25,
    '30m': 0.40,
    '1h':  0.50,
    '2h':  0.70,
    '4h':  1.00,
    '6h':  1.20,
    '8h':  1.50,
    '12h': 1.50,
    '1d':  2.00,
}

MIN_SAMPLES_MAP = {
    '5m':  15,
    '15m': 10,
    '30m': 8,
    '1h':  6,
    '2h':  5,
    '4h':  4,
    '6h':  3,
    '8h':  3,
    '12h': 3,
    '1d':  3,
}


def _resolve(tf, override, mapping, fallback):
    return override if override is not None else mapping.get(tf, fallback)


def load_settings():
    with open(os.path.join(PROJECT_ROOT, 'settings.json'), 'r') as f:
        return json.load(f)


def load_secrets():
    secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
    if not os.path.exists(secret_path):
        logger.critical("secret.json nicht gefunden!")
        sys.exit(1)
    with open(secret_path, 'r') as f:
        return json.load(f)


def fetch_history(exchange, symbol, timeframe, history_days):
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=history_days)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    logger.info(f"  Lade: {symbol} ({timeframe}) | {start_str} → {end_str}")
    df = exchange.fetch_historical_ohlcv(symbol, timeframe, start_str, end_str)

    if df is None or df.empty:
        logger.warning(f"  Keine Daten für {symbol} ({timeframe}).")
        return None

    logger.info(f"  {len(df)} Kerzen geladen")
    return df


def get_vol_factor(df) -> float:
    """ATR/ATR-MA Verhältnis als Volatilitätsfaktor für Decay-Berechnung."""
    import ta
    import pandas as pd
    try:
        atr_series = ta.volatility.AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'],
            window=14, fillna=True
        ).average_true_range()
        current_atr = float(atr_series.iloc[-1])
        atr_ma = float(atr_series.rolling(window=50, min_periods=10).mean().iloc[-1])
        return current_atr / atr_ma if atr_ma > 0 else 1.0
    except Exception:
        return 1.0


def main():
    os.makedirs(os.path.join(PROJECT_ROOT, 'artifacts', 'db'), exist_ok=True)

    parser = argparse.ArgumentParser(description="zerobot Quantum Discovery")
    parser.add_argument('--symbol',       type=str, default=None)
    parser.add_argument('--timeframe',    type=str, default=None)
    parser.add_argument('--history-days', type=int, default=None)
    parser.add_argument('--no-evolve',    action='store_true')
    args = parser.parse_args()

    logger.info("=" * 65)
    logger.info("  zerobot — Quantum Pattern Discovery (scan_and_learn.py)")
    logger.info("=" * 65)

    settings = load_settings()
    secrets = load_secrets()

    scan_cfg = settings.get('scan_settings', {})
    physics_cfg = settings.get('physics_settings', {})

    # Symbol/Timeframe-Paare aus active_strategies ableiten
    active_strategies = settings.get('live_trading_settings', {}).get('active_strategies', [])
    explicit_symbols = scan_cfg.get('symbols', [])
    explicit_timeframes = scan_cfg.get('timeframes', [])

    if explicit_symbols or explicit_timeframes:
        symbols = explicit_symbols or list(dict.fromkeys(
            s['symbol'] for s in active_strategies if s.get('symbol')
        )) or ['BTC/USDT:USDT']
        timeframes_global = explicit_timeframes or ['4h']
        scan_pairs = [(sym, tf) for sym in symbols for tf in timeframes_global]
    else:
        seen = set()
        scan_pairs = []
        for s in active_strategies:
            sym = s.get('symbol')
            tf = s.get('timeframe')
            if sym and tf and (sym, tf) not in seen:
                scan_pairs.append((sym, tf))
                seen.add((sym, tf))
        if not scan_pairs:
            scan_pairs = [('BTC/USDT:USDT', '4h')]

    history_days_override      = args.history_days or scan_cfg.get('history_days', None)
    discovery_horizon_override = scan_cfg.get('discovery_horizon', None)
    move_threshold_override    = scan_cfg.get('move_threshold_pct', None)
    min_samples_override       = scan_cfg.get('min_samples_to_activate', None)
    sequence_lengths = physics_cfg.get('sequence_lengths', [3, 4, 5])
    min_winrate      = physics_cfg.get('min_winrate', 0.45)
    min_score        = physics_cfg.get('min_score', 0.08)
    half_life_days   = physics_cfg.get('half_life_days', 180.0)

    # CLI-Filter
    if args.symbol and args.timeframe:
        scan_pairs = [(args.symbol, args.timeframe)]
    elif args.symbol:
        scan_pairs = [(args.symbol, tf) for (sym, tf) in scan_pairs if sym == args.symbol] or \
                     [(args.symbol, '4h')]
    elif args.timeframe:
        scan_pairs = [(sym, args.timeframe) for (sym, _) in scan_pairs]

    accounts = secrets.get('zerobot', [])
    if not accounts:
        logger.critical("Kein 'zerobot'-Account in secret.json.")
        sys.exit(1)

    exchange = Exchange(accounts[0])
    db = StateDB(DB_PATH)

    total_new = 0
    total_updated = 0

    for symbol, timeframe in scan_pairs:
        history_days      = _resolve(timeframe, history_days_override, HISTORY_DAYS_MAP, 730)
        discovery_horizon = _resolve(timeframe, discovery_horizon_override, DISCOVERY_HORIZON_MAP, 6)
        move_threshold    = _resolve(timeframe, move_threshold_override, MOVE_THRESHOLD_MAP, 1.0)
        min_samples       = _resolve(timeframe, min_samples_override, MIN_SAMPLES_MAP, 4)

        logger.info(f"\n{'─' * 55}")
        logger.info(
            f"  Scanne: {symbol} ({timeframe}) | "
            f"history={history_days}d | horizon={discovery_horizon} | "
            f"move≥{move_threshold}% | min_samples={min_samples}"
        )
        logger.info(f"{'─' * 55}")

        df = fetch_history(exchange, symbol, timeframe, history_days)
        if df is None:
            continue

        result = discover_states(
            df=df,
            market=symbol,
            timeframe=timeframe,
            db=db,
            sequence_lengths=sequence_lengths,
            discovery_horizon=discovery_horizon,
            move_threshold_pct=move_threshold,
        )
        total_new += result.get('new_states', 0)
        total_updated += result.get('updated_states', 0)

        if not args.no_evolve:
            vol_factor = get_vol_factor(df)
            logger.info(f"  Evolver läuft | vol_factor={vol_factor:.2f}")
            evo_result = evolve(
                db=db,
                market=symbol,
                timeframe=timeframe,
                min_samples=min_samples,
                min_winrate=min_winrate,
                score_threshold=min_score,
                half_life_days=half_life_days,
                vol_factor=vol_factor,
            )
            logger.info(
                f"  Evolver: {evo_result['activated']} aktiv, "
                f"{evo_result['deactivated']} inaktiv | "
                f"eff. Halbwertszeit: {evo_result['effective_half_life']:.0f}d"
            )

    logger.info(f"\n{'=' * 65}")
    logger.info(f"  Discovery abgeschlossen:")
    logger.info(f"  Neue States:         {total_new}")
    logger.info(f"  Aktualisierte:       {total_updated}")

    print_state_report(db)

    db.close()
    logger.info("  scan_and_learn.py abgeschlossen.")


if __name__ == "__main__":
    main()
