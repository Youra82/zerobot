# src/zerobot/analysis/portfolio_optimizer.py
import pandas as pd
from tqdm import tqdm
import sys
import os
import json
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.analysis.backtester import run_backtest
from zerobot.analysis.portfolio_simulator import (
    collect_strategy_events,
    replay_portfolio_events,
)


def _smoothed_score(strat_data, start_capital, trade_start_date, end_anchor,
                    step_days, num_samples):
    """
    Mittelt die Trailing-Performance ueber mehrere, um step_days versetzte
    Snapshot-Fenster gleicher Laenge (statt nur einen Stichtag zu messen) —
    validiert per reopt_smoothing.py (2 Tage x 7 Snapshots: Calmar 20.7 vs.
    14.1 Baseline im OOS-Test). Reduziert die Anfaelligkeit der woechentlichen
    Auswahl fuer einen Ausreisser-Trade kurz vor dem exakten Stichtag.
    Aendert NICHTS an der tatsaechlich simulierten Portfolio-Performance
    (die bleibt auf dem realen, aktuellen Fenster) — nur an der Rangfolge,
    welche Timeframe pro Symbol bevorzugt wird.
    """
    data = strat_data.get('data')
    if data is None or data.empty:
        return None
    lookback = pd.to_datetime(end_anchor, utc=True) - pd.to_datetime(trade_start_date, utc=True)
    scores = []
    for k in range(num_samples):
        anchor = pd.to_datetime(end_anchor, utc=True) - pd.Timedelta(days=k * step_days)
        window_start = anchor - lookback
        data_slice = data[data.index < anchor]
        if data_slice.empty or len(data_slice) < 100:
            continue
        try:
            res = run_backtest(data_slice.copy(), strat_data['smc_params'], strat_data['risk_params'],
                               start_capital, return_trades=False,
                               trade_start_date=window_start.isoformat())
        except Exception:
            continue
        if res and not res.get('liquidation_date'):
            scores.append(res.get('total_pnl_pct', -999.0))
    return float(np.mean(scores)) if scores else None


def run_portfolio_optimizer(start_capital, strategies_data, start_date, end_date,
                            target_max_dd: float,
                            trade_start_date: str = None,
                            oos_map: dict = None,
                            smoothing_step_days: int = 2,
                            smoothing_samples: int = 7):
    """
    Findet die beste Kombination von Strategien (Max DD <= target_max_dd, kein Coin doppelt).
    Greedy-Algorithmus — ausschließlich auf OOS-Periode (trade_start_date).

    oos_map: dict config_file → OOS-Ergebnis aus last_oos_run.json.
             Nur Strategien mit oos_pnl > 0 werden zugelassen.
    trade_start_date: Trades und Equity-Tracking erst ab diesem Datum.
    smoothing_step_days/smoothing_samples: Rangfolge der Kandidaten (welche
        Timeframe pro Symbol bevorzugt wird) basiert auf dem Mittel mehrerer
        versetzter Trailing-Snapshots statt nur dem aktuellen Stichtag.
        smoothing_samples=1 schaltet die Glaettung aus (altes Verhalten).
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

    # ── Schritt 1: Trades pro Strategie einmalig via Backtester sammeln ───────
    print("1/3: Sammle Backtester-Trades (einmalig pro Strategie)...")
    events_cache = {}  # filename → list of trade events

    for filename, strat_data in tqdm(strategies_data.items(), desc="Backtester-Trades"):
        if 'data' not in strat_data or strat_data['data'].empty:
            continue
        strategy_key = f"{strat_data['symbol']}_{strat_data['timeframe']}"
        events_cache[filename] = collect_strategy_events(
            strategy_key, strat_data, start_capital, trade_start_date)

    # ── Schritt 2: Einzel-Performance bewerten (aus gecachten Events) ─────────
    print("2/3: Analysiere Einzel-Performance...")
    use_smoothing = smoothing_samples and smoothing_samples > 1 and trade_start_date
    if use_smoothing:
        print(f"    (Rangfolge geglaettet: {smoothing_samples} Snapshots "
              f"alle {smoothing_step_days} Tage)")
    single_strategy_results = []

    for filename, strat_data in strategies_data.items():
        events = events_cache.get(filename, [])
        if not events:
            continue
        result = replay_portfolio_events(start_capital, events)
        if result and not result.get("liquidation_date"):
            actual_max_dd = result.get('max_drawdown_pct', 100.0) / 100.0
            if actual_max_dd <= target_max_dd_decimal:
                sort_score = result['end_capital']
                if use_smoothing:
                    smoothed_pnl = _smoothed_score(
                        strat_data, start_capital, trade_start_date, end_date,
                        smoothing_step_days, smoothing_samples)
                    if smoothed_pnl is not None:
                        sort_score = start_capital * (1 + smoothed_pnl / 100.0)
                single_strategy_results.append({
                    'filename':    filename,
                    'symbol':      strat_data['symbol'],
                    'timeframe':   strat_data['timeframe'],
                    'end_capital': result['end_capital'],
                    'pnl_pct':     result['total_pnl_pct'],
                    'max_dd':      result['max_drawdown_pct'],
                    'win_rate':    result['win_rate'],
                    'trade_count': result['trade_count'],
                    'sort_score':  sort_score,
                })

    if not single_strategy_results:
        print("Keine Einzelstrategie erfüllt die Bedingungen.")
        return None

    single_strategy_results.sort(key=lambda x: x['sort_score'], reverse=True)
    print(f"-> {len(single_strategy_results)} valide Einzelstrategien gefunden.")

    # ── Schritt 3: Greedy-Portfolio-Aufbau (gecachte Events, kein Re-Backtest) ─
    print("3/3: Greedy-Portfolio-Aufbau...")
    portfolio          = []
    portfolio_files    = []
    used_symbols       = set()
    best_portfolio_sim = None
    best_portfolio_pnl = float('-inf')

    for candidate in single_strategy_results:
        coin = candidate['symbol'].split('/')[0]
        if coin in used_symbols:
            continue

        test_files  = portfolio_files + [candidate['filename']]
        test_events = []
        for f in test_files:
            test_events.extend(events_cache.get(f, []))

        result = replay_portfolio_events(start_capital, test_events)
        if not result or result.get("liquidation_date"):
            continue

        actual_dd    = result.get('max_drawdown_pct', 100.0) / 100.0
        candidate_pnl = result.get('total_pnl_pct', float('-inf'))
        # Kandidat nur aufnehmen, wenn er die bisherige Portfolio-PnL nicht
        # verschlechtert — sonst verdraengt er per geteiltem Margin-Topf
        # nur bessere Trades bereits aufgenommener Strategien (siehe
        # replay_portfolio_events: Positionen konkurrieren um dieselbe Equity).
        if actual_dd <= target_max_dd_decimal and candidate_pnl >= best_portfolio_pnl:
            portfolio.append(candidate)
            portfolio_files.append(candidate['filename'])
            used_symbols.add(coin)
            best_portfolio_sim = result
            best_portfolio_pnl = candidate_pnl
            print(f"  + {candidate['symbol']} / {candidate['timeframe']} "
                  f"(PnL: {result['total_pnl_pct']:.1f}%, MaxDD: {result['max_drawdown_pct']:.1f}%)")

    # ── Entscheidung: Einzelstrategie vs. Portfolio ───────────────────────────
    best_single     = single_strategy_results[0]
    best_single_key = f"{best_single['symbol']}_{best_single['timeframe']}"
    best_single_sim = replay_portfolio_events(
        start_capital, events_cache.get(best_single['filename'], []))
    best_single_pnl = best_single_sim.get('total_pnl_pct', 0) if best_single_sim else 0

    if not portfolio_files:
        print(f"\n  ★ Kein Portfolio erfüllt MaxDD <= {target_max_dd:.0f}% — "
              f"nehme beste Einzelstrategie: {best_single['symbol']} {best_single['timeframe']} "
              f"(PnL: {best_single_pnl:.1f}%)")
        return {
            'optimal_portfolio': [best_single['filename']],
            'final_result':      best_single_sim,
        }

    portfolio_pnl = best_portfolio_sim.get('total_pnl_pct', 0) if best_portfolio_sim else 0

    if best_single_pnl > portfolio_pnl:
        print(f"\n  ★ Einzelstrategie schlägt Portfolio:")
        print(f"    {best_single['symbol']} {best_single['timeframe']}: {best_single_pnl:+.1f}%"
              f"  >  Portfolio ({len(portfolio_files)} Strategien): {portfolio_pnl:+.1f}%")
        print(f"  → Nehme Einzelstrategie.")
        return {
            'optimal_portfolio': [best_single['filename']],
            'final_result':      best_single_sim,
        }

    print(f"\n  Portfolio ({len(portfolio_files)} Strategien, {portfolio_pnl:+.1f}%) schlägt "
          f"beste Einzelstrategie ({best_single['symbol']} {best_single['timeframe']}, "
          f"{best_single_pnl:+.1f}%) → Portfolio wird verwendet.")
    return {
        'optimal_portfolio': portfolio_files,
        'final_result':      best_portfolio_sim,
    }
