import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest

try:
    import ta
except ImportError:
    ta = None

def load_configs():
    d = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
    result = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.startswith('config_') and fn.endswith('.json'):
                with open(os.path.join(d, fn)) as f:
                    result.append((fn, json.load(f)))
    return result

TREND_RR_VALUES  = [1.5, 2.0, 2.5]
RANGE_RR_VALUES  = [1.5, 2.0, 2.5, 3.0]

def main():
    parser = argparse.ArgumentParser(description='Regime-Adaptive Parameter Optimierung')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    args = parser.parse_args()

    if ta is None:
        print("FEHLER: 'ta' Paket nicht installiert. pip install ta")
        return

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    fn, cfg = configs[0]
    symbol    = cfg['market']['symbol']
    timeframe = cfg['market']['timeframe']
    strategy  = cfg.get('strategy', {})
    risk      = cfg.get('risk', {})
    orig_rr   = risk.get('risk_reward_ratio', 2.0)
    orig_risk_pct = risk.get('risk_per_trade_pct', 1.0) / 100

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 30:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    try:
        atr_ind = ta.volatility.AverageTrueRange(
            high=data['high'], low=data['low'], close=data['close'], window=14)
        data['atr'] = atr_ind.average_true_range()
        adx_ind = ta.trend.ADXIndicator(
            high=data['high'], low=data['low'], close=data['close'], window=14)
        data['adx'] = adx_ind.adx()
        data.dropna(subset=['atr', 'adx'], inplace=True)
    except Exception as e:
        print(f"Fehler bei Indikator-Berechnung: {e}")
        return

    def get_regime(adx_val):
        if adx_val > 25:
            return 'TREND'
        else:
            return 'RANGE'

    res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
    trades = res.get('trades', [])
    baseline_pnl = res.get('total_pnl_pct', 0)

    print("\n" + "=" * 70)
    print("  REGIME-ADAPTIVE PARAMETER OPTIMIERUNG")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Original RR: {orig_rr}  |  Baseline PnL: {baseline_pnl:.1f}%")
    print()

    if not trades:
        print("  Keine Trades vorhanden.")
        return

    trade_regimes = []
    for t in trades:
        try:
            ts  = pd.to_datetime(t['timestamp'], utc=True)
            idx = data.index.get_indexer([ts], method='nearest')[0]
            if idx >= 0 and idx < len(data):
                adx_val = data.iloc[idx]['adx']
                regime  = get_regime(adx_val)
            else:
                regime = 'NEUTRAL'
        except Exception:
            regime = 'NEUTRAL'
        trade_regimes.append((t, regime))

    estimated_risk = args.capital * orig_risk_pct

    print(f"  TREND vs RANGE RR-Kombinations-Grid")
    print(f"  (Simulierter PnL basierend auf originalen Win/Loss-Ergebnissen)")
    print()

    header = f"  {'TREND_RR →':<14}"
    for trr in TREND_RR_VALUES:
        header += f"  {'TRR='+str(trr):>12}"
    print(header)
    print(f"  RANGE_RR ↓")
    print(f"  {'─'*60}")

    best_pnl   = -9999
    best_combo = None

    for rrr_range in RANGE_RR_VALUES:
        row = f"  {'RRR='+str(rrr_range):<14}"
        for rrr_trend in TREND_RR_VALUES:
            total_pnl = 0.0
            for t, regime in trade_regimes:
                rr_to_use = rrr_trend if regime == 'TREND' else rrr_range
                if t['win']:
                    sim_pnl = estimated_risk * rr_to_use
                else:
                    sim_pnl = -estimated_risk
                total_pnl += sim_pnl
            sim_pnl_pct = total_pnl / args.capital * 100 if args.capital > 0 else 0
            row += f"  {sim_pnl_pct:>11.1f}%"
            if sim_pnl_pct > best_pnl:
                best_pnl   = sim_pnl_pct
                best_combo = (rrr_trend, rrr_range)
        print(row)

    print(f"  {'─'*60}")
    if best_combo:
        print(f"\n  Beste Kombination: TREND_RR={best_combo[0]}, RANGE_RR={best_combo[1]}")
        print(f"  Simulierter PnL: {best_pnl:.1f}%")
        diff = best_pnl - baseline_pnl
        print(f"  Verbesserung vs Baseline: {diff:+.1f}%")
        print()
        print(f"  Hinweis: Simulation verwendet die originalen Win/Loss-Resultate.")
        print(f"  Regime-Einteilung basiert auf ADX (> 25 = TREND, sonst RANGE).")
    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
