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
    parser = argparse.ArgumentParser(description='Slippage & Fee Impact Analysis')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    fee_levels = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
    current_fee = 0.06

    print("\n" + "=" * 75)
    print("  SLIPPAGE & FEE IMPACT ANALYSE")
    print("=" * 75)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Aktueller Bitget Taker-Fee: {current_fee}%  (markiert mit *)")
    print()

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            print(f"\n  {fn}: Keine Daten verfuegbar.")
            continue

        print(f"\n{'─'*75}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"{'─'*75}")
        print(f"  {'Fee%':<10} {'Trades':>8} {'Win%':>8} {'PnL%':>10} {'MaxDD%':>10}  {'Hinweis'}")
        print(f"  {'─'*65}")

        results = []
        for fee in fee_levels:
            res = run_backtest(data.copy(), strategy, risk, args.capital, fee_pct_override=fee)
            results.append((fee, res))

        break_even_fee = None
        for i in range(len(results) - 1):
            fee0, res0 = results[i]
            fee1, res1 = results[i + 1]
            p0 = res0.get('total_pnl_pct', 0)
            p1 = res1.get('total_pnl_pct', 0)
            if p0 >= 0 and p1 < 0:
                if (p0 - p1) != 0:
                    break_even_fee = fee0 + (fee1 - fee0) * p0 / (p0 - p1)
                else:
                    break_even_fee = (fee0 + fee1) / 2

        for fee, res in results:
            pnl    = res.get('total_pnl_pct', 0)
            wr     = res.get('win_rate', 0)
            tr     = res.get('trades_count', 0)
            dd     = res.get('max_drawdown_pct', 0) * 100
            marker = " * (Bitget)" if abs(fee - current_fee) < 0.001 else ""
            sign   = "+" if pnl > 0 else ""
            print(f"  {fee:<10.2f} {tr:>8} {wr:>7.1f}% {sign}{pnl:>9.1f}% {dd:>9.1f}%{marker}")

        print(f"  {'─'*65}")
        if break_even_fee is not None:
            print(f"  Break-Even-Fee: ~{break_even_fee:.3f}%")
        else:
            pnl_at_zero = results[0][1].get('total_pnl_pct', 0) if results else 0
            if pnl_at_zero < 0:
                print("  Strategie verliert bereits ohne Fees.")
            else:
                print("  Break-Even-Fee liegt ueber dem getesteten Bereich (> 0.20%)")

    print("\n" + "=" * 75)

if __name__ == '__main__':
    main()
