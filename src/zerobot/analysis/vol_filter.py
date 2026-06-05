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
    parser = argparse.ArgumentParser(description='Volatilitaets-Filter Optimierung')
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

    current_mvr = strategy.get('min_vol_ratio', 1.5)
    current_vfe = strategy.get('vol_filter_enabled', True)

    vol_ratios = [1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0]

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 20:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    print("\n" + "=" * 70)
    print("  VOLATILITAETS-FILTER OPTIMIERUNG")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Aktuell: vol_filter_enabled={current_vfe}, min_vol_ratio={current_mvr}")
    print()
    print(f"  {'Konfiguration':<30} {'Trades':>8} {'WR%':>8} {'PnL%':>10} {'MaxDD%':>10}  Markierung")
    print(f"  {'─'*75}")

    results_for_mark = {}

    s_no_filter = dict(strategy)
    s_no_filter['vol_filter_enabled'] = False
    res_base = run_backtest(data.copy(), s_no_filter, risk, args.capital)
    pnl_base = res_base.get('total_pnl_pct', 0)
    results_for_mark['disabled'] = pnl_base
    print(f"  {'Filter DEAKTIVIERT':<30} {res_base['trades_count']:>8} {res_base['win_rate']:>7.1f}% "
          f"{pnl_base:>9.1f}% {res_base['max_drawdown_pct']*100:>9.1f}%  (Baseline)")

    best_pnl = pnl_base
    best_label = 'disabled'

    for mvr in vol_ratios:
        s_mod = dict(strategy)
        s_mod['vol_filter_enabled'] = True
        s_mod['min_vol_ratio'] = mvr
        res = run_backtest(data.copy(), s_mod, risk, args.capital)
        pnl = res.get('total_pnl_pct', 0)
        results_for_mark[mvr] = pnl
        if pnl > best_pnl:
            best_pnl  = pnl
            best_label = mvr
        is_current = (abs(mvr - current_mvr) < 0.01 and current_vfe)
        mark = " ► aktuell" if is_current else ""
        print(f"  {'min_vol_ratio='+str(mvr):<30} {res['trades_count']:>8} {res['win_rate']:>7.1f}% "
              f"{pnl:>9.1f}% {res['max_drawdown_pct']*100:>9.1f}%{mark}")

    print(f"  {'─'*75}")
    if best_label == 'disabled':
        print(f"\n  Bester Wert: Filter DEAKTIVIERT  (PnL: {best_pnl:.1f}%)")
    else:
        print(f"\n  Bester Wert: min_vol_ratio={best_label}  (PnL: {best_pnl:.1f}%)")

    curr_pnl = results_for_mark.get(current_mvr if current_vfe else 'disabled', float('nan'))
    if not math.isnan(curr_pnl):
        diff = best_pnl - curr_pnl
        print(f"  Aktuelle Config: PnL {curr_pnl:.1f}%  (Delta zum Besten: {diff:+.1f}%)")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
