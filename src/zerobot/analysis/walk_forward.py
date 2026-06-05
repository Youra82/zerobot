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
    parser = argparse.ArgumentParser(description='Walk-Forward Out-of-Sample Test')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--windows', type=int, default=4)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden. Zuerst run_pipeline.sh ausfuehren.")
        return

    start_dt = pd.to_datetime(args.start_date, utc=True)
    end_dt   = pd.to_datetime(args.end_date, utc=True)
    total_days = (end_dt - start_dt).days
    window_days = total_days // args.windows

    windows = []
    for i in range(args.windows):
        ws = start_dt + timedelta(days=i * window_days)
        we = ws + timedelta(days=window_days) if i < args.windows - 1 else end_dt
        windows.append((ws.strftime('%Y-%m-%d'), we.strftime('%Y-%m-%d')))

    print("\n" + "=" * 70)
    print("  WALK-FORWARD OUT-OF-SAMPLE TEST")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Fenster: {args.windows}  |  Kapital: {args.capital} USDT")
    print()

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        print(f"\n{'─'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"{'─'*70}")
        header = f"  {'Fenster':<8}"
        for i in range(args.windows):
            header += f"  {'W'+str(i+1):>10}"
        print(header)

        pnl_values = []
        row_dates  = "  Zeitraum"
        row_trades = "  Trades  "
        row_wr     = "  Win%    "
        row_pnl    = "  PnL%    "

        for i, (ws, we) in enumerate(windows):
            data = load_data(symbol, timeframe, ws, we)
            if data.empty or len(data) < 20:
                row_dates  += f"  {'n/a':>10}"
                row_trades += f"  {'—':>10}"
                row_wr     += f"  {'—':>10}"
                row_pnl    += f"  {'—':>10}"
                pnl_values.append(None)
                continue
            res = run_backtest(data.copy(), strategy, risk, args.capital)
            pnl = res.get('total_pnl_pct', 0)
            pnl_values.append(pnl)
            row_dates  += f"  {(ws[2:7]):>10}"
            row_trades += f"  {res['trades_count']:>10}"
            row_wr     += f"  {res['win_rate']:>9.1f}%"
            row_pnl    += f"  {pnl:>9.1f}%"

        print(row_dates)
        print(row_trades)
        print(row_wr)
        print(row_pnl)

        valid_pnl = [v for v in pnl_values if v is not None]
        if len(valid_pnl) >= 2:
            std = float(np.std(valid_pnl))
            print(f"\n  Konsistenz (StdDev PnL): {std:.1f}%")
            if std < 10:
                print("  Bewertung: ROBUST (StdDev < 10%)")
            elif std > 25:
                print("  Bewertung: INSTABIL (StdDev > 25%)")
            else:
                print("  Bewertung: MODERAT (StdDev 10-25%)")
        else:
            print("  Nicht genug Daten fuer Konsistenzanalyse.")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
