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

def classify_regime(row, atr_ma):
    atr = row.get('atr', 0)
    adx = row.get('adx', 0)
    if pd.isna(atr) or pd.isna(adx):
        return 'NEUTRAL'
    if atr > atr_ma * 1.5:
        return 'HIGH_VOL'
    elif adx > 25:
        return 'TREND'
    elif adx < 20:
        return 'RANGE'
    else:
        return 'NEUTRAL'

def main():
    parser = argparse.ArgumentParser(description='Regime Performance Analysis')
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

    print("\n" + "=" * 70)
    print("  REGIME PERFORMANCE ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Regime: HIGH_VOL (ATR>1.5xATR_MA) | TREND (ADX>25) | RANGE (ADX<20) | NEUTRAL")
    print()

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 30:
            print(f"\n  {fn}: Keine Daten.")
            continue

        try:
            atr_ind  = ta.volatility.AverageTrueRange(
                high=data['high'], low=data['low'], close=data['close'], window=14)
            data['atr'] = atr_ind.average_true_range()

            adx_ind  = ta.trend.ADXIndicator(
                high=data['high'], low=data['low'], close=data['close'], window=14)
            data['adx'] = adx_ind.adx()

            data['atr_ma'] = data['atr'].rolling(14).mean()
            data.dropna(subset=['atr', 'adx', 'atr_ma'], inplace=True)
        except Exception as e:
            print(f"\n  {fn}: Fehler bei Indikator-Berechnung: {e}")
            continue

        data['regime'] = data.apply(
            lambda row: classify_regime(row, row['atr_ma']), axis=1)

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            print(f"\n  {fn}: Keine Trades.")
            continue

        regime_stats = {}
        for t in trades:
            try:
                ts = pd.to_datetime(t['timestamp'], utc=True)
                idx = data.index.get_indexer([ts], method='nearest')[0]
                if idx >= 0 and idx < len(data):
                    regime = data.iloc[idx]['regime']
                else:
                    regime = 'NEUTRAL'
            except Exception:
                regime = 'NEUTRAL'

            if regime not in regime_stats:
                regime_stats[regime] = {'wins': 0, 'total': 0, 'pnl_sum': 0.0}
            regime_stats[regime]['total']  += 1
            regime_stats[regime]['pnl_sum'] += t['pnl_usd']
            if t['win']:
                regime_stats[regime]['wins'] += 1

        print(f"\n{'─'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"{'─'*70}")
        print(f"  {'Regime':<12} {'Trades':>8} {'WR%':>8} {'Avg PnL':>10}  Empfehlung")
        print(f"  {'─'*55}")

        best_regime = None
        best_wr     = -1

        for regime_name in ['TREND', 'RANGE', 'HIGH_VOL', 'NEUTRAL']:
            stats = regime_stats.get(regime_name, {'wins': 0, 'total': 0, 'pnl_sum': 0.0})
            n     = stats['total']
            if n == 0:
                print(f"  {regime_name:<12} {'0':>8} {'—':>8} {'—':>10}")
                continue
            wr      = stats['wins'] / n * 100
            avg_pnl = stats['pnl_sum'] / n
            rec     = "gut" if wr > 55 else ("ok" if wr > 45 else "schlecht")
            print(f"  {regime_name:<12} {n:>8} {wr:>7.1f}% {avg_pnl:>9.2f}$  {rec}")
            if wr > best_wr:
                best_wr     = wr
                best_regime = regime_name

        if best_regime:
            print(f"\n  Empfehlung: Bestes Regime = {best_regime} (WR {best_wr:.1f}%)")

    print("\n" + "=" * 70)

if __name__ == '__main__':
    main()
