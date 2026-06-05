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
    parser = argparse.ArgumentParser(description='Multi-Timeframe Confirmation Analysis')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--window-hours', type=float, default=4)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 70)
    print("  MULTI-TIMEFRAME CONFIRMATION ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Gleichzeitigkeits-Fenster: {args.window_hours}h  |  Kapital: {args.capital} USDT")
    print()

    all_trades_by_symbol = {}

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            continue

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            continue

        for t in trades:
            t['fn'] = fn
            t['timeframe'] = timeframe
            try:
                t['ts'] = pd.to_datetime(t['timestamp'], utc=True)
            except Exception:
                t['ts'] = None

        if symbol not in all_trades_by_symbol:
            all_trades_by_symbol[symbol] = {}
        all_trades_by_symbol[symbol][timeframe] = trades

    if not all_trades_by_symbol:
        print("  Keine Trade-Daten verfuegbar.")
        return

    window_td = timedelta(hours=args.window_hours)

    for symbol, tf_dict in sorted(all_trades_by_symbol.items()):
        print(f"\n  Symbol: {symbol}")
        if len(tf_dict) < 2:
            tf = list(tf_dict.keys())[0]
            trades = tf_dict[tf]
            wr = sum(1 for t in trades if t['win']) / len(trades) * 100 if trades else 0
            print(f"    Nur ein Timeframe ({tf}): {len(trades)} Trades | WR {wr:.1f}%")
            print("    Keine Multi-TF Daten fuer dieses Symbol.")
            continue

        print(f"    Timeframes: {list(tf_dict.keys())}")
        all_trades = []
        for tf, trades in tf_dict.items():
            all_trades.extend(trades)

        valid_trades = [t for t in all_trades if t.get('ts') is not None]
        valid_trades.sort(key=lambda t: t['ts'])

        concurrent_list = []
        solo_list       = []
        used_indices    = set()

        for i, t in enumerate(valid_trades):
            if i in used_indices:
                continue
            concurrent_group = [t]
            for j, t2 in enumerate(valid_trades):
                if j == i or j in used_indices:
                    continue
                if t2['timeframe'] != t['timeframe']:
                    delta = abs((t2['ts'] - t['ts']).total_seconds()) / 3600
                    if delta <= args.window_hours:
                        concurrent_group.append(t2)
                        used_indices.add(j)
            if len(concurrent_group) > 1:
                for t in concurrent_group:
                    concurrent_list.append(t)
                used_indices.add(i)
            else:
                solo_list.append(t)

        def win_rate(lst):
            if not lst:
                return 0.0, 0
            wr = sum(1 for t in lst if t['win']) / len(lst) * 100
            return wr, len(lst)

        c_wr, c_n = win_rate(concurrent_list)
        s_wr, s_n = win_rate(solo_list)

        print(f"    {'Kategorie':<25} {'Trades':>7}  {'WR%':>7}")
        print(f"    {'─'*45}")
        print(f"    {'Gleichzeitig (Multi-TF)':<25} {c_n:>7}  {c_wr:>6.1f}%")
        print(f"    {'Solo (einzelner TF)':<25} {s_n:>7}  {s_wr:>6.1f}%")

        if c_n > 0 and s_n > 0:
            diff = c_wr - s_wr
            symbol = "+" if diff >= 0 else ""
            print(f"\n    Multi-TF Vorteil: {symbol}{diff:.1f}% WR-Differenz")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
