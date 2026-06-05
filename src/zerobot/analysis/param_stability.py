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
    parser = argparse.ArgumentParser(description='Parameter Stabilitaets-Analyse')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--windows', type=int, default=4)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    start_dt    = pd.to_datetime(args.start_date, utc=True)
    end_dt      = pd.to_datetime(args.end_date, utc=True)
    total_days  = (end_dt - start_dt).days
    window_days = total_days // args.windows

    windows = []
    for i in range(args.windows):
        ws = start_dt + timedelta(days=i * window_days)
        we = ws + timedelta(days=window_days) if i < args.windows - 1 else end_dt
        windows.append((ws.strftime('%Y-%m-%d'), we.strftime('%Y-%m-%d')))

    rr_variations = [0.85, 1.0, 1.15]

    print("\n" + "=" * 75)
    print("  PARAMETER-STABILITAETS-ANALYSE")
    print("=" * 75)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Fenster: {args.windows}  |  RR-Test: original ±15%")
    print()

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})
        orig_rr   = risk.get('risk_reward_ratio', 2.0)

        print(f"\n{'─'*75}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Original RR: {orig_rr}")
        print()

        header = f"  {'Fenster':<10}"
        for ws, we in windows:
            header += f"  {'W ('+ws[2:7]+')':>12}"
        print(header)

        row_pnl = f"  {'PnL%':<10}"
        stable_windows = 0
        window_data_cache = {}

        for ws, we in windows:
            d = load_data(symbol, timeframe, ws, we)
            window_data_cache[(ws, we)] = d
            if d.empty or len(d) < 20:
                row_pnl += f"  {'—':>12}"
                continue
            res = run_backtest(d.copy(), strategy, risk, args.capital)
            pnl = res.get('total_pnl_pct', 0)
            row_pnl += f"  {pnl:>11.1f}%"

        print(row_pnl)
        print()

        rr_labels = [f"RR={orig_rr*v:.2f}" for v in rr_variations]
        header2 = f"  {'RR-Stabilitaet':<18}"
        for ws, we in windows:
            header2 += f"  {'Best?':>12}"
        print(header2)
        print(f"  {'─'*70}")

        for var_factor in rr_variations:
            test_rr = orig_rr * var_factor
            row_s   = f"  RR={test_rr:<14.2f}"
            for ws, we in windows:
                d = window_data_cache.get((ws, we), pd.DataFrame())
                if d.empty or len(d) < 20:
                    row_s += f"  {'—':>12}"
                    continue
                best_pnl = -9999
                best_val = None
                for vf in rr_variations:
                    r_mod = dict(risk)
                    r_mod['risk_reward_ratio'] = orig_rr * vf
                    res = run_backtest(d.copy(), strategy, r_mod, args.capital)
                    p   = res.get('total_pnl_pct', -9999)
                    if p > best_pnl:
                        best_pnl = p
                        best_val = orig_rr * vf
                is_best = abs(test_rr - best_val) < 0.001 if best_val is not None else False
                row_s += f"  {'JA ✓':>12}" if is_best else f"  {'nein':>12}"
            print(row_s)

        orig_best_count = 0
        valid_w_count   = 0
        for ws, we in windows:
            d = window_data_cache.get((ws, we), pd.DataFrame())
            if d.empty or len(d) < 20:
                continue
            valid_w_count += 1
            best_pnl = -9999
            best_val = None
            for vf in rr_variations:
                r_mod = dict(risk)
                r_mod['risk_reward_ratio'] = orig_rr * vf
                res = run_backtest(d.copy(), strategy, r_mod, args.capital)
                p   = res.get('total_pnl_pct', -9999)
                if p > best_pnl:
                    best_pnl = p
                    best_val = orig_rr * vf
            if best_val is not None and abs(best_val - orig_rr) < 0.001:
                orig_best_count += 1

        if valid_w_count > 0:
            stability = orig_best_count / valid_w_count
            print(f"\n  Stabilitaets-Score: {orig_best_count}/{valid_w_count} Fenster = {stability*100:.0f}%")
            if stability >= 0.75:
                print("  Bewertung: STABIL — Original-RR ist robust")
            elif stability >= 0.5:
                print("  Bewertung: MODERAT — Original-RR haelt meist stand")
            else:
                print("  Bewertung: INSTABIL — Original-RR nicht optimal ueber Fenster")

    print("\n" + "=" * 75)

if __name__ == '__main__':
    main()
