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

def simulate_permutation(pnl_list, start_capital, rng):
    perm = rng.permutation(pnl_list)
    capital = start_capital
    peak    = start_capital
    max_dd  = 0.0
    for pnl in perm:
        capital += pnl
        if capital > peak:
            peak = capital
        if peak > 0:
            dd = (peak - capital) / peak
            if dd > max_dd:
                max_dd = dd
    final_pct = (capital - start_capital) / start_capital * 100 if start_capital > 0 else 0
    return final_pct, max_dd

def main():
    parser = argparse.ArgumentParser(description='Monte Carlo Simulation')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--simulations', type=int, default=5000)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    rng = np.random.default_rng(42)

    print("\n" + "=" * 70)
    print("  MONTE CARLO SIMULATION")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Kapital: {args.capital} USDT  |  Simulationen: {args.simulations}")
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

        res = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if len(trades) < 10:
            print(f"\n  {fn}: Zu wenige Trades ({len(trades)}) fuer Monte Carlo (min. 10).")
            continue

        pnl_list = [t['pnl_usd'] for t in trades]

        final_pcts = []
        max_dds    = []
        for _ in range(args.simulations):
            fp, md = simulate_permutation(pnl_list, args.capital, rng)
            final_pcts.append(fp)
            max_dds.append(md * 100)

        final_arr = np.array(final_pcts)
        dd_arr    = np.array(max_dds)
        ruin_prob = np.mean(final_arr < -50.0)
        median_dd = float(np.median(dd_arr))

        p5  = float(np.percentile(final_arr, 5))
        p25 = float(np.percentile(final_arr, 25))
        p50 = float(np.percentile(final_arr, 50))
        p75 = float(np.percentile(final_arr, 75))
        p95 = float(np.percentile(final_arr, 95))

        print(f"\n{'─'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Original: {len(trades)} Trades | WR {res['win_rate']:.1f}% | PnL {res['total_pnl_pct']:.1f}%")
        print(f"{'─'*70}")
        print(f"  Perzentil-Verteilung der finalen PnL%:")
        print(f"    5.  Pz (Worst-Case):  {p5:>8.1f}%")
        print(f"   25.  Pz:               {p25:>8.1f}%")
        print(f"   50.  Pz (Median):      {p50:>8.1f}%")
        print(f"   75.  Pz:               {p75:>8.1f}%")
        print(f"   95.  Pz (Best-Case):   {p95:>8.1f}%")
        print(f"\n  Ruinwahrscheinlichkeit (Kapital < 50%): {ruin_prob*100:.1f}%")
        print(f"  Median Max-Drawdown: {median_dd:.1f}%")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
