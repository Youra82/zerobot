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
    parser = argparse.ArgumentParser(description='Bootstrap Signifikanztest')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--min-trades', type=int, default=10)
    parser.add_argument('--alpha', type=float, default=0.05)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 75)
    print("  BOOTSTRAP SIGNIFIKANZTEST (Binomial, einseitig)")
    print("=" * 75)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Min. Trades: {args.min_trades}  |  Alpha: {args.alpha}")
    print(f"\n  H0: Win-Rate = 50% (Zufall)")
    print(f"  H1: Win-Rate > 50% (Edge vorhanden)")
    print()
    print(f"  {'Config':<40} {'Trades':>7} {'WR%':>7} {'Z-Score':>9} {'p-Wert':>9}  Signifikant")
    print(f"  {'─'*75}")

    significant_count = 0
    total_count       = 0

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            print(f"  {fn:<40} {'—':>7} {'—':>7} {'—':>9} {'—':>9}  n/a (keine Daten)")
            continue

        res    = run_backtest(data.copy(), strategy, risk)
        trades = res.get('trades_count', 0)
        wr_pct = res.get('win_rate', 0)

        if trades < args.min_trades:
            print(f"  {fn:<40} {trades:>7} {wr_pct:>6.1f}% {'—':>9} {'—':>9}  n/a (< {args.min_trades} Trades)")
            continue

        wins    = round(wr_pct / 100 * trades)
        n       = trades
        z       = (wins - n * 0.5) / math.sqrt(n * 0.25) if n > 0 else 0
        p_value = 0.5 * math.erfc(z / math.sqrt(2))

        is_sig  = p_value < args.alpha
        sig_str = "JA  ***" if is_sig else "nein"
        total_count += 1
        if is_sig:
            significant_count += 1

        label = fn.replace('config_', '').replace('.json', '')
        print(f"  {label:<40} {trades:>7} {wr_pct:>6.1f}% {z:>9.3f} {p_value:>9.4f}  {sig_str}")

    print(f"  {'─'*75}")
    print(f"\n  Ergebnis: {significant_count} von {total_count} Configs signifikant (alpha={args.alpha})")
    if total_count > 0:
        pct = significant_count / total_count * 100
        print(f"  ({pct:.0f}% der Configs zeigen statistisch nachweisbaren Edge)")
    print("\n" + "=" * 75)

if __name__ == '__main__':
    main()
