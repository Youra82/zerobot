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

SESSIONS = {
    'Asien  (01-09 UTC)': (1, 9),
    'Europa (09-17 UTC)': (9, 17),
    'USA    (17-01 UTC)': (17, 25),
}

def get_session(hour_utc):
    if 1 <= hour_utc < 9:
        return 'Asien  (01-09 UTC)'
    elif 9 <= hour_utc < 17:
        return 'Europa (09-17 UTC)'
    else:
        return 'USA    (17-01 UTC)'

def main():
    parser = argparse.ArgumentParser(description='Tageszeit-Analyse')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 70)
    print("  TAGESZEIT-ANALYSE (UTC)")
    print("=" * 70)
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

        session_stats = {s: {'wins': 0, 'total': 0} for s in SESSIONS}
        hour_stats    = {h: {'wins': 0, 'total': 0} for h in range(24)}

        for t in trades:
            try:
                ts   = pd.to_datetime(t['timestamp'], utc=True)
                hour = ts.hour
                sess = get_session(hour)
                hour_stats[hour]['total'] += 1
                session_stats[sess]['total'] += 1
                if t['win']:
                    hour_stats[hour]['wins'] += 1
                    session_stats[sess]['wins'] += 1
            except Exception:
                pass

        print(f"\n{'─'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Gesamt: {res['trades_count']} Trades | WR {res['win_rate']:.1f}% | PnL {res['total_pnl_pct']:.1f}%")

        print(f"\n  SESSION ANALYSE:")
        print(f"  {'Session':<30} {'Trades':>8} {'WR%':>8}  Bar")
        print(f"  {'─'*55}")
        for sess_name, (h_start, h_end) in SESSIONS.items():
            stats = session_stats[sess_name]
            n     = stats['total']
            wr    = stats['wins'] / n * 100 if n > 0 else 0
            bar   = '█' * int(wr / 5)
            print(f"  {sess_name:<30} {n:>8} {wr:>7.1f}%  {bar}")

        print(f"\n  TOP 3 BESTE STUNDEN (UTC):")
        hour_wr_list = []
        for h, stats in hour_stats.items():
            n  = stats['total']
            wr = stats['wins'] / n * 100 if n > 0 else 0
            if n >= 3:
                hour_wr_list.append((h, n, wr))
        hour_wr_list.sort(key=lambda x: x[2], reverse=True)

        if hour_wr_list:
            print(f"  {'Stunde (UTC)':<20} {'Trades':>8} {'WR%':>8}")
            print(f"  {'─'*40}")
            for h, n, wr in hour_wr_list[:3]:
                print(f"  {str(h)+':00 UTC':<20} {n:>8} {wr:>7.1f}%")
        else:
            print("  Nicht genug Trades pro Stunde (min. 3).")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
