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

RISK_PARAMS = ['risk_reward_ratio', 'risk_per_trade_pct', 'atr_multiplier_sl', 'leverage']
VARIATIONS  = [-0.30, -0.15, 0.0, +0.15, +0.30]

def ascii_bar(value, scale=1.0, width=20):
    filled = int(abs(value) / scale * width) if scale > 0 else 0
    filled = min(filled, width)
    return '█' * filled

def main():
    parser = argparse.ArgumentParser(description='Parameter Sensitivity (Tornado)')
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

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 20:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    print("\n" + "=" * 75)
    print("  PARAMETER SENSITIVITY — TORNADO-DIAGRAMM")
    print("=" * 75)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    baseline_res  = run_backtest(data.copy(), strategy, risk, args.capital)
    baseline_pnl  = baseline_res.get('total_pnl_pct', 0)
    print(f"  Baseline PnL: {baseline_pnl:.1f}%")
    print()

    sensitivity = {}
    detail_rows = {}

    for param in RISK_PARAMS:
        base_val = risk.get(param)
        if base_val is None:
            continue
        pnl_list = []
        row_parts = []
        for var in VARIATIONS:
            new_val = base_val * (1 + var)
            r_mod   = dict(risk)
            r_mod[param] = new_val
            res = run_backtest(data.copy(), strategy, r_mod, args.capital)
            pnl = res.get('total_pnl_pct', 0)
            pnl_list.append(pnl)
            row_parts.append((var, new_val, pnl))
        rng = max(pnl_list) - min(pnl_list)
        sensitivity[param] = rng
        detail_rows[param] = row_parts

    sorted_params = sorted(sensitivity.keys(), key=lambda p: sensitivity[p], reverse=True)
    max_range = max(sensitivity.values()) if sensitivity else 1.0

    print(f"  {'Parameter':<30} {'Range':>8}  {'Sensitivity Bar'}")
    print(f"  {'─'*70}")
    for param in sorted_params:
        rng = sensitivity[param]
        bar = ascii_bar(rng, scale=max_range, width=30)
        print(f"  {param:<30} {rng:>7.1f}%  {bar}")

    print()
    print(f"  Detail: PnL% bei Variation (-30% bis +30%)")
    print(f"  {'─'*75}")
    header = f"  {'Parameter':<28}"
    for var in VARIATIONS:
        label = f"{var*100:+.0f}%"
        header += f"  {label:>8}"
    print(header)
    print(f"  {'─'*75}")

    for param in sorted_params:
        row = f"  {param:<28}"
        for var, new_val, pnl in detail_rows[param]:
            marker = "*" if abs(var) < 1e-9 else " "
            row += f"  {pnl:>7.1f}%{marker}"
        print(row)

    print(f"\n  * = Baseline-Wert")
    print("\n" + "=" * 75)

if __name__ == '__main__':
    main()
