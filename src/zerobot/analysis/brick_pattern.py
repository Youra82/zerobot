import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest

def load_configs():
    d = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
    result = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.startswith('config_') and fn.endswith('.json'):
                with open(os.path.join(d, fn)) as f:
                    result.append((fn, json.load(f)))
    return result

def main():
    parser = argparse.ArgumentParser(description='Brick-Pattern Kombinationsanalyse')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    fn, cfg = configs[0]
    symbol    = cfg['market']['symbol']
    timeframe = cfg['market']['timeframe']
    strategy  = cfg.get('strategy', {})
    risk      = cfg.get('risk', {})

    current_tmb = strategy.get('trend_min_bricks', 3)
    current_rb  = strategy.get('reversal_bricks', 2)

    trend_values    = [2, 3, 4, 5, 6]
    reversal_values = [1, 2, 3]

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 20:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    print("\n" + "=" * 70)
    print("  BRICK-PATTERN KOMBINATIONSANALYSE")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Aktuell: trend_min_bricks={current_tmb}, reversal_bricks={current_rb}")
    print(f"\n  ► = aktuelle Config  |  ★ = beste Kombination")
    print()

    results = {}
    best_pnl = -9999
    best_combo = None

    for tmb in trend_values:
        for rb in reversal_values:
            s_mod = dict(strategy)
            s_mod['trend_min_bricks'] = tmb
            s_mod['reversal_bricks']  = rb
            res = run_backtest(data.copy(), s_mod, risk, args.capital)
            pnl = res.get('total_pnl_pct', -9999)
            results[(tmb, rb)] = pnl
            if pnl > best_pnl:
                best_pnl  = pnl
                best_combo = (tmb, rb)

    header = f"  {'trend_min_bricks →':<20}"
    for tmb in trend_values:
        header += f"  {'tmb='+str(tmb):>10}"
    print(header)
    print(f"  reversal_bricks ↓")
    print(f"  {'─'*70}")

    for rb in reversal_values:
        row = f"  {'rb='+str(rb):<20}"
        for tmb in trend_values:
            pnl    = results.get((tmb, rb), float('nan'))
            is_best    = (tmb, rb) == best_combo
            is_current = (tmb == current_tmb and rb == current_rb)
            if is_best and is_current:
                marker = " ►★"
            elif is_best:
                marker = "  ★"
            elif is_current:
                marker = "  ►"
            else:
                marker = "   "
            if math.isnan(pnl):
                row += f"  {'—':>10}"
            else:
                row += f"  {pnl:>7.1f}%{marker}"
        print(row)

    print(f"  {'─'*70}")
    if best_combo:
        print(f"\n  Beste Kombination: trend_min_bricks={best_combo[0]}, reversal_bricks={best_combo[1]}")
        print(f"  PnL: {best_pnl:.1f}%")
        curr_pnl = results.get((current_tmb, current_rb), float('nan'))
        if not math.isnan(curr_pnl):
            diff = best_pnl - curr_pnl
            print(f"  Aktuelle Config PnL: {curr_pnl:.1f}%  (Delta zum Besten: {diff:+.1f}%)")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
