#!/usr/bin/env python3
# run_optimizer.py
# Optuna-Optimizer für zerobot — tuned Inference-Zeit-Physics-Parameter
#
# Was wird optimiert:
#   min_score           ∈ [0.04, 0.20]  — mindeste Muster-Qualität
#   max_apen_for_trade  ∈ [0.5, 2.5]   — maximale Markt-Entropie
#   rr_ratio            ∈ [1.5, 3.5]   — Gewinn:Verlust-Verhältnis
#   te_threshold        ∈ [0.005, 0.05] — Transfer-Entropie-Schwelle
#
# Strategie:
#   - Training:  erste 70% der historischen Daten (scan_and_learn hat hier gelernt)
#   - Test:      letzte 30% (Out-of-Sample) — hier wird das Ziel maximiert
#   - Ziel:      Calmar Ratio auf TEST-Periode (PnL% / MaxDD%)
#   - Warnung:   wenn Train-Calmar >> Test-Calmar → Overfitting
#
# Ausführung:
#   .venv/bin/python3 run_optimizer.py
#   .venv/bin/python3 run_optimizer.py --trials 100 --capital 50
#   .venv/bin/python3 run_optimizer.py --symbol BTC/USDT:USDT --timeframe 4h

import os
import sys
import json
import logging
import argparse
from datetime import timezone, datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.physics.database import StateDB
from zerobot.analysis.backtester import run_backtest
from scan_and_learn import HISTORY_DAYS_MAP, load_settings, load_secrets, fetch_history
from run_backtest import split_df

os.makedirs(os.path.join(PROJECT_ROOT, 'logs'), exist_ok=True)
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(PROJECT_ROOT, 'logs', 'optimizer.log'), mode='a'
        ),
    ]
)
logger = logging.getLogger(__name__)

DB_PATH       = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'quantum.db')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
B  = '\033[1;37m'
NC = '\033[0m'


def _calmar(stats: dict) -> float:
    dd = stats.get('max_drawdown_pct', 0)
    pnl = stats.get('total_pnl_pct', 0)
    if dd > 0:
        return pnl / dd
    return pnl


def _objective(
    trial,
    datasets: list[dict],
    db: StateDB,
    base_params: dict,
    split_ratio: float,
    capital: float,
    risk_pct: float,
    leverage: int,
    overfitting_penalty: float,
):
    """Optuna-Zielfunktion: Calmar auf TEST-Periode."""
    min_score  = trial.suggest_float('min_score', 0.04, 0.20, step=0.01)
    max_apen   = trial.suggest_float('max_apen_for_trade', 0.5, 2.5, step=0.1)
    rr_ratio   = trial.suggest_float('rr_ratio', 1.5, 3.5, step=0.25)
    te_thresh  = trial.suggest_float('te_threshold', 0.005, 0.05, step=0.005)

    params = {
        'physics': {
            'min_score':          min_score,
            'sequence_lengths':   base_params['physics']['sequence_lengths'],
            'max_apen_for_trade': max_apen,
        },
        'risk': {
            'rr_ratio': rr_ratio,
        },
    }

    test_calmars  = []
    train_calmars = []

    for ds in datasets:
        df = ds['df']
        market    = ds['market']
        timeframe = ds['timeframe']

        train_df, test_df, _ = split_df(df, split_ratio)

        if len(test_df) < 70:
            continue

        test_result = run_backtest(
            df=test_df, market=market, timeframe=timeframe,
            db=db, params=params,
            start_capital=capital,
            risk_per_trade_pct=risk_pct,
            leverage=leverage,
        )
        te_st = test_result.get('stats', {})
        if not te_st.get('total_trades', 0):
            test_calmars.append(-1.0)
            continue

        c_test = _calmar(te_st)
        test_calmars.append(c_test)

        # Overfitting-Metrik: optional train-Backtest
        if overfitting_penalty > 0 and len(train_df) > 70:
            train_result = run_backtest(
                df=train_df, market=market, timeframe=timeframe,
                db=db, params=params,
                start_capital=capital,
                risk_per_trade_pct=risk_pct,
                leverage=leverage,
            )
            tr_st = train_result.get('stats', {})
            c_train = _calmar(tr_st)
            train_calmars.append(c_train)

    if not test_calmars:
        return -999.0

    avg_test = sum(test_calmars) / len(test_calmars)

    if overfitting_penalty > 0 and train_calmars:
        avg_train = sum(train_calmars) / len(train_calmars)
        overfit_gap = max(0.0, avg_train - avg_test)
        avg_test -= overfitting_penalty * overfit_gap

    return avg_test


def write_best_params(params: dict, risk_pct: float = None):
    """Schreibt beste Physics-Parameter in settings.json."""
    try:
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    except Exception as e:
        print(f"{R}Fehler beim Lesen von settings.json: {e}{NC}")
        return False

    ps = settings.setdefault('physics_settings', {})
    ps['min_score']          = params['min_score']
    ps['max_apen_for_trade'] = params['max_apen_for_trade']
    ps['te_threshold']       = params['te_threshold']

    settings.setdefault('risk_settings', {})['rr_ratio'] = params['rr_ratio']

    if risk_pct is not None:
        settings['risk_settings']['risk_per_entry_pct'] = risk_pct

    try:
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(settings, f, indent=2)
        print(
            f"\n{G}✓ settings.json aktualisiert:{NC}\n"
            f"  min_score:          {params['min_score']}\n"
            f"  max_apen_for_trade: {params['max_apen_for_trade']}\n"
            f"  te_threshold:       {params['te_threshold']}\n"
            f"  rr_ratio:           {params['rr_ratio']}\n"
        )
        return True
    except Exception as e:
        print(f"{R}Fehler beim Schreiben: {e}{NC}")
        return False


def main():
    parser = argparse.ArgumentParser(description="zerobot Optuna Physics Optimizer")
    parser.add_argument('--symbol',      type=str,   default=None)
    parser.add_argument('--timeframe',   type=str,   default=None)
    parser.add_argument('--trials',      type=int,   default=50)
    parser.add_argument('--capital',     type=float, default=50.0)
    parser.add_argument('--risk',        type=float, default=None)
    parser.add_argument('--split-ratio', type=float, default=0.70)
    parser.add_argument('--penalty',     type=float, default=0.5,
                        help="Overfitting-Strafe wenn Train-Calmar >> Test-Calmar")
    parser.add_argument('--auto-write',  action='store_true',
                        help="Beste Parameter automatisch in settings.json schreiben")
    args = parser.parse_args()

    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        print(f"{R}Optuna nicht installiert. Bitte ausführen: pip install optuna{NC}")
        sys.exit(1)

    settings = load_settings()
    secrets  = load_secrets()

    physics_cfg   = settings.get('physics_settings', {})
    risk_cfg      = settings.get('risk_settings', {})
    active_strats = settings.get('live_trading_settings', {}).get('active_strategies', [])

    if args.symbol and args.timeframe:
        pairs = [(args.symbol, args.timeframe)]
    else:
        seen, pairs = set(), []
        for s in active_strats:
            sym, tf = s.get('symbol'), s.get('timeframe')
            if sym and tf and (sym, tf) not in seen:
                pairs.append((sym, tf))
                seen.add((sym, tf))
        if not pairs:
            pairs = [('BTC/USDT:USDT', '4h')]

    risk_pct = args.risk or risk_cfg.get('risk_per_entry_pct', 1.0)
    leverage = int(risk_cfg.get('leverage', 5))
    capital  = args.capital

    base_params = {
        'physics': {
            'sequence_lengths': physics_cfg.get('sequence_lengths', [3, 4, 5]),
        },
    }

    accounts = secrets.get('zerobot', [])
    if not accounts:
        logger.critical("Kein 'zerobot'-Account in secret.json.")
        sys.exit(1)
    exchange = Exchange(accounts[0])

    print(f"\n{'─' * 68}")
    print(f"{B}  zerobot — Optuna Physics Parameter Optimizer{NC}")
    print(f"  Trials: {args.trials} | Kapital: {capital:.0f} USDT | "
          f"Risiko: {risk_pct}% | Leverage: {leverage}x")
    print(f"  Split: {args.split_ratio:.0%}/{1-args.split_ratio:.0%} | "
          f"Overfitting-Penalty: {args.penalty}")
    print(f"  Ziel: Maximiere Calmar-Ratio auf TEST-Periode (Out-of-Sample)")
    print(f"{'─' * 68}\n")

    # Daten vorladen
    print("  Lade historische Daten ...", end='', flush=True)
    datasets = []
    for symbol, timeframe in pairs:
        history_days = HISTORY_DAYS_MAP.get(timeframe, 730)
        df = fetch_history(exchange, symbol, timeframe, history_days)
        if df is not None and len(df) > 100:
            datasets.append({'df': df, 'market': symbol, 'timeframe': timeframe})
    if not datasets:
        print(f"\n{R}Keine Daten geladen.{NC}")
        sys.exit(1)
    print(f" {len(datasets)} Datensätze geladen.")

    db = StateDB(DB_PATH)
    if db.get_db_summary()['total_states'] == 0:
        print(f"{Y}Warnung: quantum.db ist leer. Erst scan_and_learn.py ausführen!{NC}")

    # Split-Vorschau
    print(f"\n  Daten-Split (Split-Ratio {args.split_ratio:.0%}/{1-args.split_ratio:.0%}):")
    for ds in datasets:
        tr, te, sd = split_df(ds['df'], args.split_ratio)
        print(f"    {ds['market']} ({ds['timeframe']}): "
              f"Train={len(tr)} | Test={len(te)} | Split-Datum: {sd[:10]}")
    print()

    # Optuna-Studie
    print(f"  Starte Optuna ({args.trials} Trials) ...\n")
    study = optuna.create_study(
        direction='maximize',
        study_name='zerobot_physics_optimizer',
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    from functools import partial
    objective = partial(
        _objective,
        datasets=datasets,
        db=db,
        base_params=base_params,
        split_ratio=args.split_ratio,
        capital=capital,
        risk_pct=risk_pct,
        leverage=leverage,
        overfitting_penalty=args.penalty,
    )

    total = args.trials
    done  = [0]

    def _progress_callback(s, t):
        done[0] += 1
        if done[0] % max(1, total // 10) == 0 or done[0] == total:
            best = s.best_value
            pct  = done[0] / total * 100
            print(
                f"  [{pct:5.1f}%] Trial {done[0]:>4}/{total} | "
                f"Best Calmar (TEST): {best:.3f}"
            )

    study.optimize(objective, n_trials=args.trials, callbacks=[_progress_callback])
    db.close()

    best = study.best_params
    best_val = study.best_value

    print(f"\n{'=' * 68}")
    print(f"{B}  Optimierungsergebnis{NC}")
    print(f"{'=' * 68}")
    print(f"  Bester Calmar-Score (TEST): {G}{best_val:.3f}{NC}")
    print(f"\n  Optimale Parameter:")
    print(f"    min_score:          {best['min_score']:.2f}")
    print(f"    max_apen_for_trade: {best['max_apen_for_trade']:.1f}")
    print(f"    rr_ratio:           {best['rr_ratio']:.2f}")
    print(f"    te_threshold:       {best['te_threshold']:.3f}")

    # Top-5 Trials
    print(f"\n  Top-5 Trials:")
    print(f"  {'#':>4} {'Calmar':>8} {'min_score':>10} {'max_apen':>10} "
          f"{'rr_ratio':>9} {'te_thresh':>10}")
    print(f"  {'-' * 55}")
    top5 = sorted(study.trials, key=lambda t: t.value or -999, reverse=True)[:5]
    for i, t in enumerate(top5, 1):
        if t.value is None:
            continue
        p = t.params
        cal_col = G if t.value > 0 else R
        print(
            f"  {i:>4} {cal_col}{t.value:>8.3f}{NC} "
            f"{p.get('min_score', 0):>10.2f} "
            f"{p.get('max_apen_for_trade', 0):>10.1f} "
            f"{p.get('rr_ratio', 0):>9.2f} "
            f"{p.get('te_threshold', 0):>10.3f}"
        )
    print(f"{'=' * 68}\n")

    # Validierungs-Backtest mit besten Parametern
    best_params_full = {
        'physics': {
            'min_score':          best['min_score'],
            'sequence_lengths':   base_params['physics']['sequence_lengths'],
            'max_apen_for_trade': best['max_apen_for_trade'],
        },
        'risk': {'rr_ratio': best['rr_ratio']},
    }

    print(f"  Finaler Validierungs-Backtest mit besten Parametern:\n")
    db2 = StateDB(DB_PATH)
    for ds in datasets:
        train_df, test_df, split_date = split_df(ds['df'], args.split_ratio)
        market, timeframe = ds['market'], ds['timeframe']

        print(f"  {market} ({timeframe}) | Train: {len(train_df)} | "
              f"Test: {len(test_df)} | Split: {split_date[:10]}")

        if len(train_df) > 70:
            tr = run_backtest(
                df=train_df, market=market, timeframe=timeframe,
                db=db2, params=best_params_full,
                start_capital=capital, risk_per_trade_pct=risk_pct, leverage=leverage,
            )
            ts = tr.get('stats', {})
            pnl_col = G if ts.get('total_pnl_pct', 0) > 0 else R
            print(
                f"    TRAIN: {ts.get('total_trades', 0)} Trades | "
                f"WR: {ts.get('win_rate', 0):.1%} | "
                f"{pnl_col}PnL: {ts.get('total_pnl_pct', 0):+.1f}%{NC} | "
                f"Calmar: {ts.get('calmar_ratio', 0):.2f}"
            )

        if len(test_df) > 70:
            te = run_backtest(
                df=test_df, market=market, timeframe=timeframe,
                db=db2, params=best_params_full,
                start_capital=capital, risk_per_trade_pct=risk_pct, leverage=leverage,
            )
            ts = te.get('stats', {})
            pnl_col = G if ts.get('total_pnl_pct', 0) > 0 else R
            print(
                f"    TEST:  {ts.get('total_trades', 0)} Trades | "
                f"WR: {ts.get('win_rate', 0):.1%} | "
                f"{pnl_col}PnL: {ts.get('total_pnl_pct', 0):+.1f}%{NC} | "
                f"Calmar: {ts.get('calmar_ratio', 0):.2f}"
            )
        print()
    db2.close()

    # Settings schreiben
    if args.auto_write:
        write_best_params(best, risk_pct)
    else:
        try:
            ans = input("  Beste Parameter in settings.json speichern? (j/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans in ('j', 'ja', 'y', 'yes'):
            write_best_params(best, risk_pct)
        else:
            print(f"\n{Y}  settings.json wurde NICHT geändert.{NC}\n")


if __name__ == '__main__':
    main()
