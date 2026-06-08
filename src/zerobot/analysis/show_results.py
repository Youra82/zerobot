# src/zerobot/analysis/show_results.py
import os
import sys
import json
import pandas as pd
from datetime import date
import logging
import argparse

logging.getLogger('tensorflow').setLevel(logging.ERROR)
logging.getLogger('absl').setLevel(logging.ERROR)
import warnings
warnings.filterwarnings('ignore')

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.analysis.backtester import load_data, run_backtest
from zerobot.analysis.portfolio_simulator import run_portfolio_simulation
from zerobot.analysis.portfolio_optimizer import run_portfolio_optimizer
from zerobot.utils.telegram import send_document

GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
NC     = '\033[0m'


def _get_telegram_cfg():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), 'r') as f:
            s = json.load(f)
        tg = s.get('telegram', {})
        return tg.get('bot_token', ''), tg.get('chat_id', '')
    except Exception:
        return '', ''


def run_single_analysis(start_date, end_date, start_capital):
    print("--- ZeroBot Ergebnis-Analyse (Einzel-Modus) ---")
    configs_dir  = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
    all_results  = []

    if not os.path.exists(configs_dir):
        print(f"Konfigurationsverzeichnis nicht gefunden: {configs_dir}")
        return

    config_files = sorted([f for f in os.listdir(configs_dir)
                            if f.startswith('config_') and f.endswith('.json')])
    if not config_files:
        print("\nKeine Konfigurationen gefunden.")
        return

    print(f"Zeitraum: {start_date} bis {end_date} | Startkapital: {start_capital} USDT")

    for filename in config_files:
        config_path = os.path.join(configs_dir, filename)
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            symbol    = config['market']['symbol']
            timeframe = config['market']['timeframe']
            print(f"\nAnalysiere: {filename}...")

            # Per-Config Warmup aus _meta.train_start — gleiche Logik wie Mode 3.
            # Stellt korrekten EAR-Brick-State an der OOS-Grenze sicher.
            per_warmup = config.get('_meta', {}).get('train_start', start_date)
            data = load_data(symbol, timeframe, per_warmup, end_date)
            if data.empty:
                print(f"--> WARNUNG: Keine Daten für {symbol} ({timeframe}). Überspringe.")
                continue

            strategy_params = config.get('strategy', {})
            risk_params     = config.get('risk', {})
            strategy_params['symbol']    = symbol
            strategy_params['timeframe'] = timeframe
            strategy_params['htf']       = config['market'].get('htf')

            result = run_backtest(data.copy(), strategy_params, risk_params, start_capital,
                                  verbose=False, trade_start_date=start_date)
            all_results.append({
                "Strategie":  f"{symbol} ({timeframe})",
                "Trades":     result.get('trades_count', 0),
                "Win Rate %": result.get('win_rate', 0),
                "PnL %":      result.get('total_pnl_pct', -100),
                "Max DD %":   result.get('max_drawdown_pct', 1.0) * 100,
                "Endkapital": result.get('end_capital', start_capital),
                "EAR base%":  round(strategy_params.get('base_pct', 0) * 100, 3),
                "Hebel":      risk_params.get('leverage', '?'),
                "SL ATR":     round(risk_params.get('atr_multiplier_sl', 0), 2),
                "RRR":        round(risk_params.get('risk_reward_ratio', 0), 2),
                "Trail Akt.": round(risk_params.get('trailing_stop_activation_rr', 0), 2),
                "Trail CB%":  round(risk_params.get('trailing_stop_callback_rate_pct', 0), 2),
            })
        except Exception as e:
            print(f"--> FEHLER bei {filename}: {e}")
            continue

    if not all_results:
        print("\nKeine gültigen Ergebnisse.")
        return

    results_df = pd.DataFrame(all_results).sort_values(by="PnL %", ascending=False)
    pd.set_option('display.width', 1200)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.float_format', '{:.2f}'.format)
    print("\n\n" + "=" * 120)
    print("                       ZeroBot EAR — Einzelstrategien")
    print("=" * 120)
    print(results_df.to_string(index=False))
    print("=" * 120)


def run_shared_mode(is_auto: bool, start_date, end_date, start_capital, target_max_dd: float):
    mode_name = "Automatische Portfolio-Optimierung" if is_auto else "Manuelle Portfolio-Simulation"
    print(f"--- ZeroBot {mode_name} ---")

    configs_dir          = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
    available_strategies = []
    if os.path.isdir(configs_dir):
        for filename in sorted(os.listdir(configs_dir)):
            if filename.startswith('config_') and filename.endswith('.json'):
                available_strategies.append(filename)

    if not available_strategies:
        print("Keine optimierten Strategien (Configs) gefunden.")
        return

    selected_files = []
    if not is_auto:
        print("\nVerfügbare Strategien:")
        for i, name in enumerate(available_strategies):
            print(f"  {i+1}) {name}")
        selection = input("\nWelche Strategien? (Zahlen mit Komma oder 'alle'): ")
        try:
            if selection.lower() == 'alle':
                selected_files = available_strategies
            else:
                selected_files = [available_strategies[int(i.strip()) - 1]
                                  for i in selection.split(',')]
        except (ValueError, IndexError):
            print("Ungültige Auswahl.")
            return
    else:
        selected_files = available_strategies

    strategies_data = {}
    for filename in selected_files:
        try:
            with open(os.path.join(configs_dir, filename), 'r') as f:
                config = json.load(f)
            symbol     = config['market']['symbol']
            timeframe  = config['market']['timeframe']
            per_warmup = config.get('_meta', {}).get('train_start', start_date)
            data       = load_data(symbol, timeframe, per_warmup, end_date)
            if not data.empty:
                strategies_data[filename] = {
                    'symbol':      symbol,
                    'timeframe':   timeframe,
                    'data':        data,
                    'smc_params':  config.get('strategy', {}),
                    'risk_params': config.get('risk', {}),
                    'htf':         config['market'].get('htf'),
                }
        except Exception as e:
            print(f"FEHLER beim Laden von {filename}: {e}")

    if not strategies_data:
        print("Keine Daten geladen.")
        return

    try:
        if is_auto:
            results = run_portfolio_optimizer(start_capital, strategies_data, start_date, end_date, target_max_dd)
            if results and results.get('final_result'):
                final_report   = results['final_result']
                portfolio_files = results.get('optimal_portfolio', [])
                print("\n" + "=" * 60)
                print("     Optimales Portfolio")
                print("=" * 60)
                for f in portfolio_files:
                    print(f"  - {f}")
                pnl = final_report.get('total_pnl_pct', 0)
                print(f"\n  Endkapital:  {final_report['end_capital']:.2f} USDT")
                print(f"  PnL:         {pnl:+.2f}%")
                print(f"  Max DD:      {final_report['max_drawdown_pct']:.2f}%")
                print(f"  Win Rate:    {final_report['win_rate']:.1f}%")
                print(f"  Trades:      {final_report['trade_count']}")
            else:
                print(f"\nKein Portfolio erfüllt Max DD <= {target_max_dd:.1f}%.")
        else:
            sim_data = {v['symbol'] + "_" + v['timeframe']: v for v in strategies_data.values()}
            results  = run_portfolio_simulation(start_capital, sim_data, start_date, end_date,
                                               trade_start_date=start_date)
            if results:
                print("\n" + "=" * 60)
                print("     Portfolio-Simulations-Ergebnis")
                print("=" * 60)
                print(f"  Endkapital:  {results['end_capital']:.2f} USDT")
                print(f"  PnL:         {results['total_pnl_pct']:+.2f}%")
                print(f"  Max DD:      {results['max_drawdown_pct']:.2f}%")
                print(f"  Win Rate:    {results['win_rate']:.1f}%")
                print(f"  Trades:      {results['trade_count']}")
    except Exception as e:
        print(f"\nFEHLER: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='1', type=str, choices=['1', '2', '3'],
                        help="1=Einzel, 2=Manuell, 3=Auto")
    parser.add_argument('--target_max_drawdown', default=30.0, type=float)
    args = parser.parse_args()

    print("\n--- Bitte Konfiguration festlegen ---")
    start_date    = input("Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: ") or "2023-01-01"
    end_date      = input("Enddatum   (JJJJ-MM-TT) [Standard: Heute]: ")       or date.today().strftime("%Y-%m-%d")
    start_capital = int(input("Startkapital in USDT [Standard: 1000]: ")        or 1000)

    if args.mode == '2':
        run_shared_mode(False, start_date, end_date, start_capital, 999.0)
    elif args.mode == '3':
        run_shared_mode(True, start_date, end_date, start_capital, args.target_max_drawdown)
    else:
        run_single_analysis(start_date=start_date, end_date=end_date, start_capital=start_capital)
