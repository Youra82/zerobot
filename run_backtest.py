#!/usr/bin/env python3
# run_backtest.py
# Quantum-State-Backtest mit 70/30 Train/Test-Split
#
# Ablauf:
#   1. Daten laden (voller Zeitraum aus HISTORY_DAYS_MAP)
#   2. Split-Datum berechnen (70% Train / 30% Test)
#   3. Backtest auf TRAIN-Periode (In-Sample — bekannte DB-Patterns)
#   4. Backtest auf TEST-Periode  (Out-of-Sample — echter Validierungscheck)
#   5. Vergleichstabelle ausgeben
#
# Ausführung:
#   .venv/bin/python3 run_backtest.py
#   .venv/bin/python3 run_backtest.py --symbol BTC/USDT:USDT --timeframe 4h
#   .venv/bin/python3 run_backtest.py --split-ratio 0.7 --capital 50

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.physics.database import StateDB
from zerobot.analysis.backtester import run_backtest, save_results, print_backtest_summary
from scan_and_learn import (
    HISTORY_DAYS_MAP, load_settings, load_secrets, fetch_history,
)

os.makedirs(os.path.join(PROJECT_ROOT, 'logs'), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(PROJECT_ROOT, 'logs', 'backtest.log'), mode='a'
        ),
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'quantum.db')

G   = '\033[0;32m'
Y   = '\033[1;33m'
R   = '\033[0;31m'
C   = '\033[0;36m'
B   = '\033[1;37m'
NC  = '\033[0m'

TIMEFRAME_MINUTES = {
    '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120,
    '4h': 240, '6h': 360, '8h': 480, '12h': 720, '1d': 1440,
}


def split_df(df: pd.DataFrame, split_ratio: float = 0.70) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Teilt einen DataFrame in Train/Test anhand des Split-Verhältnisses."""
    n = len(df)
    split_idx = int(n * split_ratio)
    train_df  = df.iloc[:split_idx]
    test_df   = df.iloc[split_idx:]
    split_date = str(df.index[split_idx]) if split_idx < n else "Ende"
    return train_df, test_df, split_date


def filter_df_by_date(df: pd.DataFrame, start: str = None, end: str = None) -> pd.DataFrame:
    if start:
        df = df[df.index >= pd.Timestamp(start, tz='UTC')]
    if end:
        df = df[df.index <= pd.Timestamp(end + ' 23:59:59', tz='UTC')]
    return df


def main():
    parser = argparse.ArgumentParser(description="zerobot Quantum Backtester (70/30)")
    parser.add_argument('--symbol',      type=str,   default=None)
    parser.add_argument('--timeframe',   type=str,   default=None)
    parser.add_argument('--capital',     type=float, default=50.0)
    parser.add_argument('--risk',        type=float, default=None,
                        help="Risiko pro Trade in % (überschreibt settings.json)")
    parser.add_argument('--split-ratio', type=float, default=0.70,
                        help="Train/Test-Verhältnis (default: 0.70)")
    parser.add_argument('--test-only',   action='store_true',
                        help="Nur Test-Periode backtesten (schneller)")
    parser.add_argument('--start-date',  type=str,   default=None,
                        help="Manuelles Startdatum (YYYY-MM-DD)")
    parser.add_argument('--end-date',    type=str,   default=None,
                        help="Manuelles Enddatum (YYYY-MM-DD)")
    args = parser.parse_args()

    settings = load_settings()
    secrets  = load_secrets()

    physics_cfg = settings.get('physics_settings', {})
    risk_cfg    = settings.get('risk_settings', {})
    active_strats = settings.get('live_trading_settings', {}).get('active_strategies', [])

    risk_pct = args.risk or risk_cfg.get('risk_per_entry_pct', 1.0)
    leverage = int(risk_cfg.get('leverage', 5))
    capital  = args.capital

    params = {
        'physics': {
            'min_score':          physics_cfg.get('min_score', 0.08),
            'sequence_lengths':   physics_cfg.get('sequence_lengths', [3, 4, 5]),
            'max_apen_for_trade': physics_cfg.get('max_apen_for_trade', 1.5),
        },
        'risk': {
            'rr_ratio': risk_cfg.get('rr_ratio', 2.0),
        },
    }

    accounts = secrets.get('zerobot', [])
    if not accounts:
        logger.critical("Kein 'zerobot'-Account in secret.json gefunden.")
        sys.exit(1)
    exchange = Exchange(accounts[0])

    db = StateDB(DB_PATH)

    # Pairs bestimmen: --symbol/--timeframe > quantum.db > settings.json
    if args.symbol and args.timeframe:
        pairs = [(args.symbol, args.timeframe)]
    else:
        db_pairs = db.get_all_market_pairs()
        if db_pairs:
            pairs = db_pairs
        else:
            seen, pairs = set(), []
            for s in active_strats:
                sym, tf = s.get('symbol'), s.get('timeframe')
                if sym and tf and (sym, tf) not in seen:
                    pairs.append((sym, tf))
                    seen.add((sym, tf))
            if not pairs:
                pairs = [('BTC/USDT:USDT', '4h')]

    split_ratio = args.split_ratio
    print(f"\n{'─' * 66}")
    print(f"{B}  zerobot — Quantum State Backtest (70/30 Train/Test){NC}")
    print(f"  Kapital: {capital:.0f} USDT | Risiko: {risk_pct}% | "
          f"Split: {split_ratio:.0%}/{1-split_ratio:.0%} | Leverage: {leverage}x")
    print(f"  Min Score: {params['physics']['min_score']} | "
          f"Max ApEn: {params['physics']['max_apen_for_trade']} | "
          f"R:R = 1:{params['risk']['rr_ratio']}")
    print(f"{'─' * 66}\n")

    all_stats = []

    for symbol, timeframe in pairs:
        history_days = HISTORY_DAYS_MAP.get(timeframe, 730)
        df = fetch_history(exchange, symbol, timeframe, history_days)
        if df is None:
            continue

        if args.start_date or args.end_date:
            df = filter_df_by_date(df, args.start_date, args.end_date)
        if df.empty:
            logger.warning(f"Keine Daten im Zeitraum für {symbol}.")
            continue

        train_df, test_df, split_date = split_df(df, split_ratio)

        logger.info(
            f"\n{symbol} ({timeframe}) | Gesamt: {len(df)} Kerzen | "
            f"Train: {len(train_df)} | Test: {len(test_df)} | Split: {split_date}"
        )

        train_stats = {}
        if not args.test_only and len(train_df) > 70:
            train_results = run_backtest(
                df=train_df, market=symbol, timeframe=timeframe,
                db=db, params=params,
                start_capital=capital, risk_per_trade_pct=risk_pct, leverage=leverage,
            )
            print_backtest_summary(train_results, symbol, timeframe, label=f"TRAIN {split_ratio:.0%}")
            save_results(train_results, symbol, f"{timeframe}_train")
            train_stats = train_results.get('stats', {})

        test_stats = {}
        if len(test_df) > 70:
            test_results = run_backtest(
                df=test_df, market=symbol, timeframe=timeframe,
                db=db, params=params,
                start_capital=capital, risk_per_trade_pct=risk_pct, leverage=leverage,
            )
            print_backtest_summary(test_results, symbol, timeframe, label=f"TEST {1-split_ratio:.0%}")
            save_results(test_results, symbol, f"{timeframe}_test")
            test_stats = test_results.get('stats', {})

        all_stats.append((symbol, timeframe, train_stats, test_stats))

    db.close()

    if not all_stats:
        print(f"{R}Keine Ergebnisse.{NC}")
        return

    # ─── Vergleichstabelle ───────────────────────────────────────────────────
    w = 76
    print(f"\n{'=' * w}")
    print(f"{B}  TRAIN vs. TEST — Gesamtübersicht{NC}")
    print(f"{'=' * w}")
    print(
        f"{C}  {'Symbol':<22} {'TF':<5} {'Periode':<8} "
        f"{'Trades':>7} {'WR':>7} {'PnL%':>8} {'PF':>6} {'Calmar':>7}{NC}"
    )
    print(f"  {'-' * (w - 2)}")

    for sym, tf, tr_st, te_st in all_stats:
        for label, st in [('TRAIN', tr_st), ('TEST', te_st)]:
            if not st.get('total_trades'):
                continue
            wr     = st['win_rate']
            pnl    = st['total_pnl_pct']
            pf     = st.get('profit_factor', 0)
            cal    = st.get('calmar_ratio', 0)
            n      = st['total_trades']
            sign   = '+' if pnl >= 0 else ''
            pf_str = f"{pf:.2f}" if pf != float('inf') else "∞"
            cal_str = f"{cal:.2f}" if cal != float('inf') else "∞"

            pnl_col = G if pnl > 0 else (Y if pnl == 0 else R)
            wr_col  = G if wr >= 0.50 else (Y if wr >= 0.43 else R)
            lbl_col = Y if label == 'TRAIN' else C

            print(
                f"  {sym:<22} {tf:<5} {lbl_col}{label:<8}{NC} "
                f"{n:>7} "
                f"{wr_col}{wr:>6.1%}{NC} "
                f"{pnl_col}{sign}{pnl:>6.1f}%{NC} "
                f"{pf_str:>6} "
                f"{cal_str:>7}"
            )

    print(f"{'=' * w}")

    # Overfitting-Warnung
    for sym, tf, tr_st, te_st in all_stats:
        tr_wr = tr_st.get('win_rate', 0)
        te_wr = te_st.get('win_rate', 0)
        if tr_wr > 0 and te_wr > 0 and (tr_wr - te_wr) > 0.15:
            print(
                f"\n{Y}  Warnung Overfitting: {sym} ({tf}) — "
                f"Train WR {tr_wr:.1%} vs. Test WR {te_wr:.1%} "
                f"(Δ={tr_wr - te_wr:.1%} > 15%){NC}"
            )
        elif te_wr >= 0.50:
            tr_cal = tr_st.get('calmar_ratio', 0)
            te_cal = te_st.get('calmar_ratio', 0)
            print(
                f"\n{G}  OK: {sym} ({tf}) — "
                f"Test WR {te_wr:.1%} | Test Calmar {te_cal:.2f}{NC}"
            )

    print()


if __name__ == '__main__':
    main()
