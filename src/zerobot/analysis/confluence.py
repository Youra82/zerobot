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
    parser = argparse.ArgumentParser(description='Confluence Score Analyse')
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
    print("  CONFLUENCE SCORE ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Gleichzeitigkeits-Fenster: {args.window_hours}h  |  Kapital: {args.capital} USDT")
    print()

    all_trades = []

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
        for t in trades:
            try:
                t['ts'] = pd.to_datetime(t['timestamp'], utc=True)
                t['fn'] = fn
                all_trades.append(t)
            except Exception:
                pass

    if not all_trades:
        print("  Keine Trades gefunden.")
        return

    all_trades.sort(key=lambda t: t['ts'])
    window_td = timedelta(hours=args.window_hours)

    confluence_groups = {}
    assigned = [False] * len(all_trades)

    for i, t in enumerate(all_trades):
        if assigned[i]:
            continue
        group = [t]
        assigned[i] = True
        for j in range(i + 1, len(all_trades)):
            if assigned[j]:
                continue
            t2 = all_trades[j]
            if (t2['ts'] - t['ts']) > window_td:
                break
            if t2['side'] == t['side']:
                delta = abs((t2['ts'] - t['ts']).total_seconds()) / 3600
                if delta <= args.window_hours:
                    group.append(t2)
                    assigned[j] = True
        count = len(group)
        key   = min(count, 3)
        if key not in confluence_groups:
            confluence_groups[key] = []
        confluence_groups[key].extend(group)

    print(f"  {'Confluence':<20} {'Trades':>8} {'Wins':>8} {'WR%':>8}  Bewertung")
    print(f"  {'─'*60}")

    for signal_count in [1, 2, 3]:
        group = confluence_groups.get(signal_count, [])
        label = f"{signal_count} Signal{'e' if signal_count > 1 else ''}" if signal_count < 3 else "3+ Signale"
        if not group:
            print(f"  {label:<20} {'—':>8} {'—':>8} {'—':>8}  keine Daten")
            continue
        total = len(group)
        wins  = sum(1 for t in group if t['win'])
        wr    = wins / total * 100 if total > 0 else 0
        if wr > 60:
            bew = "SEHR GUT"
        elif wr > 50:
            bew = "GUT"
        elif wr > 40:
            bew = "NEUTRAL"
        else:
            bew = "SCHLECHT"
        print(f"  {label:<20} {total:>8} {wins:>8} {wr:>7.1f}%  {bew}")

    print(f"  {'─'*60}")
    print(f"\n  Gesamt-Trades analysiert: {len(all_trades)}")

    if len(confluence_groups.get(1, [])) > 0 and len(confluence_groups.get(2, [])) > 0:
        wr1 = sum(1 for t in confluence_groups[1] if t['win']) / len(confluence_groups[1]) * 100
        wr2 = sum(1 for t in confluence_groups[2] if t['win']) / len(confluence_groups[2]) * 100
        diff = wr2 - wr1
        print(f"  Confluence-Effekt (2 vs 1 Signal): {diff:+.1f}% WR-Verbesserung")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
