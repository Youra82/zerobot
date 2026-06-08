# src/zerobot/analysis/portfolio_optimizer.py
import pandas as pd
from tqdm import tqdm
import sys
import os
import json
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.analysis.portfolio_simulator import run_portfolio_simulation


def run_portfolio_optimizer(start_capital, strategies_data, start_date, end_date,
                            target_max_dd: float,
                            trade_start_date: str = None,
                            oos_map: dict = None):
    """
    Findet die beste Kombination von Strategien (Max DD <= target_max_dd, kein Coin doppelt).
    Greedy-Algorithmus — ausschließlich auf OOS-Periode (trade_start_date).

    oos_map: dict config_file → OOS-Ergebnis aus last_oos_run.json.
             Nur Strategien mit oos_pnl > 0 werden zugelassen.
    trade_start_date: Trades und Equity-Tracking erst ab diesem Datum.
    """
    print(f"\n--- Starte Portfolio-Optimierung: Max DD <= {target_max_dd:.2f}% & ohne Coin-Kollisionen ---")
    target_max_dd_decimal = target_max_dd / 100.0

    if not strategies_data:
        print("Keine Strategien zum Optimieren gefunden.")
        return None

    # OOS-Filter: nur Strategien mit positivem OOS-PnL zulassen
    if oos_map:
        before = len(strategies_data)
        strategies_data = {
            k: v for k, v in strategies_data.items()
            if oos_map.get(k, {}).get('oos_pnl', -999) > 0
        }
        print(f"  OOS-Filter: {before} Configs → {len(strategies_data)} mit positivem OOS-PnL")
        if not strategies_data:
            print("  Keine Strategie hatte positiven OOS-PnL — Optimierung abgebrochen.")
            return None

    print("1/3: Analysiere Einzel-Performance...")
    single_strategy_results = []

    for filename, strat_data in tqdm(strategies_data.items(), desc="Bewerte Einzelstrategien"):
        strategy_key = f"{strat_data['symbol']}_{strat_data['timeframe']}"
        sim_data     = {strategy_key: strat_data}
        if 'data' not in strat_data or strat_data['data'].empty:
            continue

        result = run_portfolio_simulation(
            start_capital, sim_data, start_date, end_date,
            verbose=False, trade_start_date=trade_start_date)
        if result and not result.get("liquidation_date"):
            actual_max_dd = result.get('max_drawdown_pct', 100.0) / 100.0
            if actual_max_dd <= target_max_dd_decimal:
                single_strategy_results.append({
                    'filename':    filename,
                    'symbol':      strat_data['symbol'],
                    'timeframe':   strat_data['timeframe'],
                    'end_capital': result['end_capital'],
                    'pnl_pct':     result['total_pnl_pct'],
                    'max_dd':      result['max_drawdown_pct'],
                    'win_rate':    result['win_rate'],
                    'trade_count': result['trade_count'],
                })

    if not single_strategy_results:
        print("Keine Einzelstrategie erfüllt die Bedingungen.")
        return None

    single_strategy_results.sort(key=lambda x: x['end_capital'], reverse=True)
    print(f"-> {len(single_strategy_results)} valide Einzelstrategien gefunden.")

    print("2/3: Greedy-Portfolio-Aufbau...")
    portfolio          = []
    portfolio_files    = []
    used_symbols       = set()
    best_portfolio_sim = None

    for candidate in single_strategy_results:
        coin = candidate['symbol'].split('/')[0]
        if coin in used_symbols:
            continue

        test_portfolio_files = portfolio_files + [candidate['filename']]
        test_sim_data_keyed  = {
            f"{strategies_data[f]['symbol']}_{strategies_data[f]['timeframe']}": strategies_data[f]
            for f in test_portfolio_files if f in strategies_data
        }

        result = run_portfolio_simulation(
            start_capital, test_sim_data_keyed, start_date, end_date,
            verbose=False, trade_start_date=trade_start_date)
        if not result or result.get("liquidation_date"):
            continue

        actual_dd = result.get('max_drawdown_pct', 100.0) / 100.0
        if actual_dd <= target_max_dd_decimal:
            portfolio.append(candidate)
            portfolio_files.append(candidate['filename'])
            used_symbols.add(coin)
            best_portfolio_sim = result
            print(f"  + {candidate['symbol']} / {candidate['timeframe']} "
                  f"(PnL: {result['total_pnl_pct']:.1f}%, MaxDD: {result['max_drawdown_pct']:.1f}%)")

    print("3/3: Finalisiere...")

    if not portfolio_files:
        best_single = single_strategy_results[0]
        key         = f"{best_single['symbol']}_{best_single['timeframe']}"
        final_sim   = run_portfolio_simulation(
            start_capital, {key: strategies_data[best_single['filename']]},
            start_date, end_date, verbose=False, trade_start_date=trade_start_date)
        return {
            'optimal_portfolio': [best_single['filename']],
            'final_result':      final_sim,
        }

    return {
        'optimal_portfolio': portfolio_files,
        'final_result':      best_portfolio_sim,
    }
