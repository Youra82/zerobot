# src/zerobot/analysis/optimizer.py
# Renko-Strategie Parameter-Optimierung via Optuna
import os
import sys
import json
import optuna
import numpy as np
import argparse
import logging
import warnings
from datetime import datetime as _dt

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
warnings.filterwarnings('ignore')

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.analysis.backtester import load_data, run_backtest
from zerobot.utils.timeframe_utils import determine_htf

optuna.logging.set_verbosity(optuna.logging.WARNING)

HISTORICAL_DATA         = None
CURRENT_SYMBOL          = None
CURRENT_TIMEFRAME       = None
CURRENT_HTF             = None
MAX_DRAWDOWN_CONSTRAINT = 0.30
MIN_WIN_RATE_CONSTRAINT = 45.0
MIN_PNL_CONSTRAINT      = 0.0
START_CAPITAL           = 100
OPTIM_MODE              = "strict"
FIXED_ATR_MULTIPLIER    = None   # wenn gesetzt → Optuna überspringt diese Variable
FIXED_TREND_MIN_BRICKS  = None
FIXED_REVERSAL_BRICKS   = None
FIXED_VOL_FILTER        = None

RESULTS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_optimizer_run.json')


def create_safe_filename(symbol, timeframe):
    return f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"


def objective(trial):
    atr_mult    = FIXED_ATR_MULTIPLIER   if FIXED_ATR_MULTIPLIER   is not None else trial.suggest_float('atr_multiplier', 0.5, 3.0)
    trend_min   = FIXED_TREND_MIN_BRICKS if FIXED_TREND_MIN_BRICKS is not None else trial.suggest_int('trend_min_bricks', 2, 6)
    reversal    = FIXED_REVERSAL_BRICKS  if FIXED_REVERSAL_BRICKS  is not None else trial.suggest_int('reversal_bricks', 1, 3)
    vol_filter  = FIXED_VOL_FILTER       if FIXED_VOL_FILTER       is not None else trial.suggest_categorical('vol_filter_enabled', [True, False])
    strategy_params = {
        'atr_multiplier':     atr_mult,
        'trend_min_bricks':   trend_min,
        'reversal_bricks':    reversal,
        'vol_filter_enabled': vol_filter,
        'min_vol_ratio':      trial.suggest_float('min_vol_ratio', 1.0, 2.5),
        'symbol':    CURRENT_SYMBOL,
        'timeframe': CURRENT_TIMEFRAME,
        'htf':       CURRENT_HTF,
    }
    risk_params = {
        'risk_reward_ratio':              trial.suggest_float('risk_reward_ratio',         1.5, 5.0),
        'risk_per_trade_pct':             trial.suggest_float('risk_per_trade_pct',        0.5, 3.0),
        'leverage':                       trial.suggest_int('leverage',                    5,  20),
        'trailing_stop_activation_rr':    trial.suggest_float('trailing_stop_activation_rr', 1.0, 3.0),
        'trailing_stop_callback_rate_pct':trial.suggest_float('trailing_stop_callback_rate_pct', 0.2, 2.0),
        'atr_multiplier_sl':              trial.suggest_float('atr_multiplier_sl',         1.5, 5.0),
        'min_sl_pct': 0.3,
    }

    result   = run_backtest(HISTORICAL_DATA.copy(), strategy_params, risk_params, START_CAPITAL, verbose=False)
    pnl      = result.get('total_pnl_pct', -1000)
    drawdown = result.get('max_drawdown_pct', 1.0)
    trades   = result.get('trades_count', 0)
    win_rate = result.get('win_rate', 0)

    if OPTIM_MODE == "strict" and (
        drawdown > MAX_DRAWDOWN_CONSTRAINT or win_rate < MIN_WIN_RATE_CONSTRAINT
        or pnl < MIN_PNL_CONSTRAINT or trades < 15
    ):
        raise optuna.exceptions.TrialPruned()
    elif OPTIM_MODE == "best_profit" and (drawdown > MAX_DRAWDOWN_CONSTRAINT or trades < 15):
        raise optuna.exceptions.TrialPruned()

    return pnl


def main():
    global HISTORICAL_DATA, CURRENT_SYMBOL, CURRENT_TIMEFRAME, CURRENT_HTF
    global MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, START_CAPITAL, OPTIM_MODE
    global FIXED_ATR_MULTIPLIER, FIXED_TREND_MIN_BRICKS, FIXED_REVERSAL_BRICKS, FIXED_VOL_FILTER

    parser = argparse.ArgumentParser(description="Parameter-Optimierung für ZeroBot (Renko)")
    parser.add_argument('--symbols',      type=str, default="")
    parser.add_argument('--timeframes',   type=str, default="")
    parser.add_argument('--pairs',        type=str, default="")
    parser.add_argument('--start_date',   required=True, type=str)
    parser.add_argument('--end_date',     required=True, type=str)
    parser.add_argument('--jobs',         required=True, type=int)
    parser.add_argument('--max_drawdown', required=True, type=float)
    parser.add_argument('--start_capital',required=True, type=float)
    parser.add_argument('--min_win_rate', required=True, type=float)
    parser.add_argument('--trials',       required=True, type=int)
    parser.add_argument('--min_pnl',      required=True, type=float)
    parser.add_argument('--mode',         required=True, type=str)
    parser.add_argument('--fixed-atr-multiplier',    type=float,  default=None)
    parser.add_argument('--fixed-trend-min-bricks',  type=int,    default=None)
    parser.add_argument('--fixed-reversal-bricks',   type=int,    default=None)
    parser.add_argument('--fixed-vol-filter',        type=str,    default=None,
                        help='"true" oder "false"')
    args = parser.parse_args()

    MAX_DRAWDOWN_CONSTRAINT = args.max_drawdown / 100.0
    MIN_WIN_RATE_CONSTRAINT = args.min_win_rate
    MIN_PNL_CONSTRAINT      = args.min_pnl
    START_CAPITAL           = args.start_capital
    N_TRIALS                = args.trials
    OPTIM_MODE              = args.mode
    FIXED_ATR_MULTIPLIER    = args.fixed_atr_multiplier
    FIXED_TREND_MIN_BRICKS  = args.fixed_trend_min_bricks
    FIXED_REVERSAL_BRICKS   = args.fixed_reversal_bricks
    FIXED_VOL_FILTER        = (args.fixed_vol_filter.lower() == 'true') \
                              if args.fixed_vol_filter is not None else None

    fixed_info = []
    if FIXED_ATR_MULTIPLIER   is not None: fixed_info.append(f"atr_multiplier={FIXED_ATR_MULTIPLIER}")
    if FIXED_TREND_MIN_BRICKS is not None: fixed_info.append(f"trend_min_bricks={FIXED_TREND_MIN_BRICKS}")
    if FIXED_REVERSAL_BRICKS  is not None: fixed_info.append(f"reversal_bricks={FIXED_REVERSAL_BRICKS}")
    if FIXED_VOL_FILTER       is not None: fixed_info.append(f"vol_filter={FIXED_VOL_FILTER}")
    if fixed_info:
        print(f"  [INFO] Fixierte Parameter: {', '.join(fixed_info)}")

    if args.pairs.strip():
        TASKS = []
        for p in args.pairs.strip().split():
            sym, tf = p.rsplit('|', 1)
            TASKS.append({'symbol': sym, 'timeframe': tf})
    elif args.symbols and args.timeframes:
        symbols    = args.symbols.split()
        timeframes = args.timeframes.split()
        TASKS = [{'symbol': f"{s}/USDT:USDT", 'timeframe': tf}
                 for s in symbols for tf in timeframes]
    else:
        print("Fehler: --pairs oder --symbols + --timeframes muss angegeben werden.")
        return

    run_results = {
        'run_start': _dt.now().isoformat(timespec='seconds'),
        'run_end':   None,
        'saved':     [],
        'failed':    [],
    }

    for task in TASKS:
        symbol, timeframe = task['symbol'], task['timeframe']
        CURRENT_SYMBOL    = symbol
        CURRENT_TIMEFRAME = timeframe
        CURRENT_HTF       = determine_htf(timeframe)

        print(f"\n===== Optimiere: {symbol} ({timeframe}) [Renko] =====")

        HISTORICAL_DATA = load_data(symbol, timeframe, args.start_date, args.end_date)
        if HISTORICAL_DATA.empty:
            print("  Keine Daten verfügbar.")
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'no_data'})
            continue

        DB_FILE      = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'optuna_studies_zerobot.db')
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        STORAGE_URL  = f"sqlite:///{DB_FILE}?timeout=60"
        study_name   = f"renko_{create_safe_filename(symbol, timeframe)}_{OPTIM_MODE}"

        study = optuna.create_study(
            storage=STORAGE_URL, study_name=study_name,
            direction="maximize", load_if_exists=True)
        try:
            study.optimize(objective, n_trials=N_TRIALS, n_jobs=args.jobs, show_progress_bar=True)
        except Exception as e:
            print(f"FEHLER: {e}")
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': str(e)[:80]})
            continue

        valid_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if not valid_trials:
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'no_valid_trials'})
            continue

        best_trial  = max(valid_trials, key=lambda t: t.value)
        best_params = best_trial.params
        new_pnl     = best_trial.value

        config_dir         = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
        os.makedirs(config_dir, exist_ok=True)
        config_filename    = f'config_{create_safe_filename(symbol, timeframe)}.json'
        config_output_path = os.path.join(config_dir, config_filename)

        existing_pnl = None
        if os.path.exists(config_output_path):
            try:
                with open(config_output_path) as cf:
                    existing_cfg = json.load(cf)
                existing_pnl = existing_cfg.get('_meta', {}).get('pnl_pct')
            except Exception:
                pass

        if existing_pnl is not None and new_pnl <= existing_pnl:
            print(f"  Bestehende Config besser ({existing_pnl:.2f}% vs {new_pnl:.2f}%) — wird nicht überschrieben.")
            run_results['failed'].append({
                'symbol': symbol, 'timeframe': timeframe,
                'reason': f'existing_better_{existing_pnl:.2f}pct',
            })
            continue

        strategy_config = {
            'atr_multiplier':     round(best_params['atr_multiplier'], 3),
            'trend_min_bricks':   best_params['trend_min_bricks'],
            'reversal_bricks':    best_params['reversal_bricks'],
            'vol_filter_enabled': best_params['vol_filter_enabled'],
            'min_vol_ratio':      round(best_params['min_vol_ratio'], 2),
        }
        risk_config = {
            'margin_mode':                     "isolated",
            'risk_per_trade_pct':              round(best_params['risk_per_trade_pct'], 2),
            'risk_reward_ratio':               round(best_params['risk_reward_ratio'], 2),
            'leverage':                        best_params['leverage'],
            'trailing_stop_activation_rr':     round(best_params['trailing_stop_activation_rr'], 2),
            'trailing_stop_callback_rate_pct': round(best_params['trailing_stop_callback_rate_pct'], 2),
            'atr_multiplier_sl':               round(best_params['atr_multiplier_sl'], 2),
            'min_sl_pct':                      0.3,
        }
        behavior_config = {"use_longs": True, "use_shorts": True}

        config_output = {
            "market":   {"symbol": symbol, "timeframe": timeframe, "htf": CURRENT_HTF},
            "strategy": strategy_config,
            "risk":     risk_config,
            "behavior": behavior_config,
            "_meta": {
                "pnl_pct":      round(new_pnl, 2),
                "optimized_at": _dt.now().isoformat(timespec='seconds'),
            },
        }
        with open(config_output_path, 'w') as f:
            json.dump(config_output, f, indent=4)
        print(f"\n[OK] Beste Konfiguration gespeichert: {config_filename}")

        run_results['saved'].append({
            'symbol':      symbol,
            'timeframe':   timeframe,
            'pnl_pct':     round(new_pnl, 2),
            'config_file': config_filename,
        })

    run_results['run_end'] = _dt.now().isoformat(timespec='seconds')
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(run_results, f, indent=2, ensure_ascii=False)
    print(f"\nErgebnisse gespeichert: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
