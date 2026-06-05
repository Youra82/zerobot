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

PARAM_RANGES = {
    'rr':       ('risk_reward_ratio',             [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]),
    'atr_sl':   ('atr_multiplier_sl',             [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]),
    'trailing': ('trailing_stop_callback_rate_pct',[0.3, 0.5, 0.8, 1.0, 1.5, 2.0]),
}

def main():
    parser = argparse.ArgumentParser(description='Parameter Walk-Forward Sweep')
    parser.add_argument('--param', choices=['rr', 'atr_sl', 'trailing'], default='rr')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--windows', type=int, default=3)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    param_key, param_values = PARAM_RANGES[args.param]
    fn, cfg = configs[0]

    symbol    = cfg['market']['symbol']
    timeframe = cfg['market']['timeframe']
    strategy  = cfg.get('strategy', {})
    risk      = cfg.get('risk', {})

    start_dt   = pd.to_datetime(args.start_date, utc=True)
    end_dt     = pd.to_datetime(args.end_date, utc=True)
    total_days = (end_dt - start_dt).days
    window_days = total_days // args.windows

    windows = []
    for i in range(args.windows):
        ws = start_dt + timedelta(days=i * window_days)
        we = ws + timedelta(days=window_days) if i < args.windows - 1 else end_dt
        windows.append((ws.strftime('%Y-%m-%d'), we.strftime('%Y-%m-%d')))

    print("\n" + "=" * 70)
    print(f"  PARAMETER WALK-FORWARD: {param_key}")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Fenster: {args.windows}")
    print()

    all_data = {}
    for ws, we in windows:
        d = load_data(symbol, timeframe, ws, we)
        all_data[(ws, we)] = d

    print(f"  {'Param-Wert':<14}", end='')
    for i, (ws, we) in enumerate(windows):
        print(f"  {'W'+str(i+1)+' ('+ws[2:7]+')':>14}", end='')
    print(f"  {'Avg PnL%':>10}")
    print(f"  {'─'*70}")

    results_by_val = {}
    current_val = risk.get(param_key, None)

    for val in param_values:
        r_mod = dict(risk)
        r_mod[param_key] = val
        window_pnls = []
        row = f"  {val:<14}"
        for ws, we in windows:
            d = all_data[(ws, we)]
            if d.empty or len(d) < 20:
                row += f"  {'—':>14}"
                continue
            res  = run_backtest(d.copy(), strategy, r_mod, args.capital)
            pnl  = res.get('total_pnl_pct', 0)
            window_pnls.append(pnl)
            row += f"  {pnl:>13.1f}%"
        avg = float(np.mean(window_pnls)) if window_pnls else float('nan')
        results_by_val[val] = avg
        marker = " <-- aktuell" if current_val is not None and abs(val - current_val) < 1e-9 else ""
        row += f"  {avg:>9.1f}%{marker}"
        print(row)

    print(f"  {'─'*70}")
    valid = {v: a for v, a in results_by_val.items() if not math.isnan(a)}
    if valid:
        best_val = max(valid, key=lambda v: valid[v])
        print(f"\n  Bester Out-of-Sample Wert: {param_key} = {best_val}  (Avg PnL: {valid[best_val]:.1f}%)")
        if current_val is not None:
            curr_avg = valid.get(current_val, float('nan'))
            if not math.isnan(curr_avg):
                diff = valid[best_val] - curr_avg
                print(f"  Aktueller Wert {current_val}: Avg PnL {curr_avg:.1f}%  (Delta: {diff:+.1f}%)")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
