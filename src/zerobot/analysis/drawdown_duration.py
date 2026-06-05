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

def analyze_drawdowns(trades, start_capital):
    if not trades:
        return []

    sorted_trades = sorted(trades, key=lambda t: t['timestamp'])
    equity = [start_capital]
    timestamps = [None]
    for t in sorted_trades:
        equity.append(equity[-1] + t['pnl_usd'])
        timestamps.append(t['timestamp'])

    dd_periods = []
    peak_idx   = 0
    peak_val   = equity[0]
    in_dd      = False
    dd_start   = 0

    for i in range(1, len(equity)):
        val = equity[i]
        if val > peak_val:
            if in_dd:
                depth = (peak_val - min(equity[dd_start:i])) / peak_val * 100 if peak_val > 0 else 0
                try:
                    ts_start = pd.to_datetime(timestamps[dd_start], utc=True)
                    ts_end   = pd.to_datetime(timestamps[i], utc=True)
                    dur_days = (ts_end - ts_start).total_seconds() / 86400
                except Exception:
                    dur_days = 0
                trough_idx = dd_start + int(np.argmin(equity[dd_start:i]))
                dd_periods.append({
                    'start':   timestamps[dd_start],
                    'bottom':  timestamps[trough_idx],
                    'end':     timestamps[i],
                    'depth':   depth,
                    'dur_days': dur_days,
                })
                in_dd = False
            peak_val = val
            peak_idx = i
        elif val < peak_val and not in_dd:
            in_dd    = True
            dd_start = peak_idx

    if in_dd and len(equity) > dd_start:
        depth = (peak_val - min(equity[dd_start:])) / peak_val * 100 if peak_val > 0 else 0
        try:
            ts_start = pd.to_datetime(timestamps[dd_start], utc=True)
            ts_end   = pd.to_datetime(timestamps[-1], utc=True)
            dur_days = (ts_end - ts_start).total_seconds() / 86400
        except Exception:
            dur_days = 0
        trough_idx = dd_start + int(np.argmin(equity[dd_start:]))
        dd_periods.append({
            'start':   timestamps[dd_start],
            'bottom':  timestamps[trough_idx],
            'end':     None,
            'depth':   depth,
            'dur_days': dur_days,
        })

    return dd_periods

def main():
    parser = argparse.ArgumentParser(description='Drawdown Duration Analysis')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 80)
    print("  DRAWDOWN DURATION ANALYSE")
    print("=" * 80)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            print(f"\n  {fn}: Keine Daten.")
            continue

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            print(f"\n  {fn}: Keine Trades.")
            continue

        dd_periods = analyze_drawdowns(trades, args.capital)

        print(f"\n{'─'*80}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Trades: {res['trades_count']} | PnL: {res['total_pnl_pct']:.1f}% | Max DD: {res['max_drawdown_pct']*100:.1f}%")
        print()

        if not dd_periods:
            print("  Keine Drawdown-Perioden gefunden (nur Gewinne).")
            continue

        print(f"  {'#':<4} {'Start':<12} {'Tief':<12} {'Ende':<12} {'Tiefe%':>8} {'Dauer(T)':>10}")
        print(f"  {'─'*65}")
        for i, dd in enumerate(dd_periods[:10], 1):
            start_str  = str(dd['start'])[:10] if dd['start'] else '—'
            bottom_str = str(dd['bottom'])[:10] if dd['bottom'] else '—'
            end_str    = str(dd['end'])[:10] if dd['end'] else 'offen'
            print(f"  {i:<4} {start_str:<12} {bottom_str:<12} {end_str:<12} {dd['depth']:>7.1f}% {dd['dur_days']:>9.1f}")

        if len(dd_periods) > 10:
            print(f"  ... und {len(dd_periods)-10} weitere Drawdown-Perioden")

        durations = [dd['dur_days'] for dd in dd_periods]
        depths    = [dd['depth']    for dd in dd_periods]

        print(f"\n  Statistik ({len(dd_periods)} Drawdown-Perioden):")
        print(f"    Avg Dauer:       {np.mean(durations):>7.1f} Tage")
        print(f"    Max Dauer:       {np.max(durations):>7.1f} Tage")
        print(f"    90. Pz Dauer:    {np.percentile(durations, 90):>7.1f} Tage")
        print(f"    Avg Tiefe:       {np.mean(depths):>7.1f}%")
        print(f"    Max Tiefe:       {np.max(depths):>7.1f}%")

    print("\n" + "=" * 80)

if __name__ == '__main__':
    main()
