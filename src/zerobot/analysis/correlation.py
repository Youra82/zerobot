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

def pearson_corr(x, y):
    x = np.array(x, dtype=float)
    y = np.array(y, dtype=float)
    if len(x) < 2:
        return float('nan')
    mx, my = x.mean(), y.mean()
    num = ((x - mx) * (y - my)).sum()
    den = math.sqrt(((x - mx)**2).sum() * ((y - my)**2).sum())
    return num / den if den > 0 else float('nan')

def main():
    parser = argparse.ArgumentParser(description='Anti-Korrelations-Portfolio Analyse')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 70)
    print("  ANTI-KORRELATIONS-PORTFOLIO ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    if len(configs) == 1:
        print("  Nur eine Config — kein Portfolio-Vergleich moeglich.")
        return

    weekly_series = {}
    labels = []

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

        records = []
        for t in trades:
            try:
                ts = pd.to_datetime(t['timestamp'], utc=True)
                records.append({'week': ts.isocalendar()[0] * 100 + ts.isocalendar()[1],
                                'pnl_usd': t['pnl_usd']})
            except Exception:
                pass

        if not records:
            continue

        df = pd.DataFrame(records)
        weekly = df.groupby('week')['pnl_usd'].sum()
        label  = fn.replace('config_', '').replace('.json', '')[:20]
        weekly_series[label] = weekly
        labels.append(label)

    if len(labels) < 2:
        print("  Zu wenige Configs mit Trade-Daten fuer Korrelationsanalyse.")
        return

    all_weeks = sorted(set().union(*[set(s.index) for s in weekly_series.values()]))
    matrix_data = {}
    for label in labels:
        s = weekly_series[label]
        matrix_data[label] = [s.get(w, 0.0) for w in all_weeks]

    col_w = max(len(l) for l in labels) + 2

    print(f"  Korrelationsmatrix (woechentliches PnL)")
    print()
    header = f"  {' ':<{col_w}}"
    for l in labels:
        header += f"  {l[:10]:>12}"
    print(header)
    print(f"  {'─'*70}")

    corr_pairs = []
    for i, l1 in enumerate(labels):
        row = f"  {l1:<{col_w}}"
        for j, l2 in enumerate(labels):
            if i == j:
                row += f"  {'1.000':>12}"
            elif j < i:
                c = pearson_corr(matrix_data[l1], matrix_data[l2])
                row += f"  {c:>12.3f}"
                corr_pairs.append((c, l1, l2))
            else:
                c = pearson_corr(matrix_data[l1], matrix_data[l2])
                row += f"  {c:>12.3f}"
        print(row)

    print(f"  {'─'*70}")

    if corr_pairs:
        valid_pairs = [(c, l1, l2) for c, l1, l2 in corr_pairs if not math.isnan(c)]
        if valid_pairs:
            best = min(valid_pairs, key=lambda x: x[0])
            print(f"\n  Bestes Anti-Korrelations-Paar: {best[1]} + {best[2]}")
            print(f"  Korrelation: {best[0]:.3f}")
            if best[0] < -0.3:
                print("  Bewertung: GUTE Diversifikation (r < -0.3)")
            elif best[0] < 0.3:
                print("  Bewertung: NEUTRALE Korrelation")
            else:
                print("  Bewertung: HOHE Korrelation — wenig Diversifikation")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
