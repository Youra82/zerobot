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
    parser = argparse.ArgumentParser(description='Kelly Position Sizing')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 80)
    print("  KELLY POSITION SIZING ANALYSE")
    print("=" * 80)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()
    print(f"  Kelly% = (WR * RRR - (1-WR)) / RRR")
    print(f"  Half-Kelly = Kelly / 2  (empfohlene konservative Positionsgroesse)")
    print()
    print(f"  {'Config':<35} {'WR%':>7} {'RRR':>6} {'Kelly%':>9} {'HalfKelly%':>11} {'Aktuell%':>10}  Empfehlung")
    print(f"  {'─'*95}")

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})
        current_risk_pct = risk.get('risk_per_trade_pct', 1.0)
        rrr              = risk.get('risk_reward_ratio', 2.0)

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            label = fn.replace('config_', '').replace('.json', '')[:35]
            print(f"  {label:<35} {'—':>7} {rrr:>6.2f} {'—':>9} {'—':>11} {current_risk_pct:>9.1f}%  keine Daten")
            continue

        res = run_backtest(data.copy(), strategy, risk, args.capital)
        wr  = res.get('win_rate', 0) / 100

        kelly      = (wr * rrr - (1 - wr)) / rrr if rrr > 0 else 0
        half_kelly = kelly / 2

        label = fn.replace('config_', '').replace('.json', '')[:35]

        if kelly < 0:
            recommendation = "NICHT EMPFEHLENSWERT — negatives Kelly"
        elif half_kelly < current_risk_pct * 0.5:
            recommendation = "UEBERHOEHTES Risiko — reduzieren"
        elif half_kelly < current_risk_pct:
            recommendation = "Leicht reduzieren empfohlen"
        elif half_kelly > current_risk_pct * 2:
            recommendation = "Risiko koeonte erhoeht werden"
        else:
            recommendation = "OK — nahe Half-Kelly"

        print(f"  {label:<35} {wr*100:>6.1f}% {rrr:>6.2f} {kelly*100:>8.1f}% {half_kelly*100:>10.1f}% {current_risk_pct:>9.1f}%  {recommendation}")

        if kelly < 0:
            print(f"  {'':35}  --> Mathematisch nicht empfehlenswert zu traden")
            print(f"  {'':35}      (Negatives Kelly = Erwartungswert < 0)")

    print(f"\n  {'─'*95}")
    print("\n  Hinweis: Half-Kelly ist der empfohlene Wert fuer reales Trading.")
    print("  Negatives Kelly bedeutet: System verliert langfristig — kein Edge vorhanden.")
    print("\n" + "=" * 80)

if __name__ == '__main__':
    main()
