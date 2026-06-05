#!/usr/bin/env python3
"""
run_portfolio_optimizer.py  (zerobot)

Lädt alle Renko-Configs, führt Portfolio-Simulation durch und wählt das beste
Portfolio per Greedy-Algorithmus. Schreibt active_strategies in settings.json.

Aufruf:
  python3 run_portfolio_optimizer.py              # interaktiv
  python3 run_portfolio_optimizer.py --auto-write # automatisch (Scheduler)
  python3 run_portfolio_optimizer.py --replot     # Replot aktives Portfolio
"""
import contextlib
import io
import os
import sys
import json
import argparse
from datetime import date, timedelta
from tqdm import tqdm

PROJECT_ROOT  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

CONFIGS_DIR   = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')

B  = '\033[1;37m'
G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
NC = '\033[0m'

DEFAULT_LOOKBACK_DAYS = 730

BOT_NAME = 'zerobot'


def _scan_configs() -> list:
    if not os.path.isdir(CONFIGS_DIR):
        return []
    return sorted([
        os.path.join(CONFIGS_DIR, f)
        for f in os.listdir(CONFIGS_DIR)
        if f.endswith('.json')
    ])


def _build_strategies_data(config_files: list, start_date: str, end_date: str) -> dict:
    from zerobot.analysis.backtester import load_data
    strategies_data = {}
    for path in tqdm(config_files, desc='Lade Configs & Daten'):
        fname = os.path.basename(path)
        try:
            with open(path) as f:
                config = json.load(f)
            market    = config.get('market', {})
            symbol    = market.get('symbol', '')
            timeframe = market.get('timeframe', '')
            htf       = market.get('htf')
            if not symbol or not timeframe:
                continue
            data = load_data(symbol, timeframe, start_date, end_date)
            if data is None or data.empty or len(data) < 50:
                print(f"  {Y}Übersprungen (keine Daten): {fname}{NC}")
                continue
            strategies_data[fname] = {
                'symbol':      symbol,
                'timeframe':   timeframe,
                'data':        data,
                'smc_params':  config.get('strategy', {}),
                'risk_params': config.get('risk', {}),
                'htf':         htf,
            }
        except Exception as e:
            print(f"  {Y}Fehler bei {fname}: {e}{NC}")
    return strategies_data


def _write_to_settings(portfolio_files: list, strategies_data: dict) -> None:
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    existing     = settings.get('live_trading_settings', {}).get('active_strategies', [])
    existing_map = {(s.get('symbol'), s.get('timeframe')): s for s in existing}
    new_strategies = []
    for fname in portfolio_files:
        sd        = strategies_data.get(fname, {})
        symbol    = sd.get('symbol', '')
        timeframe = sd.get('timeframe', '')
        if not symbol or not timeframe:
            continue
        base  = existing_map.get((symbol, timeframe), {})
        entry = {**base, 'symbol': symbol, 'timeframe': timeframe, 'active': True}
        new_strategies.append(entry)
    lt = settings.setdefault('live_trading_settings', {})
    lt['active_strategies']          = new_strategies
    lt['use_auto_optimizer_results'] = True
    with open(SETTINGS_PATH, 'w') as f:
        json.dump(settings, f, indent=4)


def _get_telegram_creds():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            s = json.load(f)
        tg = s.get('telegram', {})
        t, c = tg.get('bot_token', ''), tg.get('chat_id', '')
        return (t, c) if t and c else (None, None)
    except Exception:
        return None, None


def _send_telegram(msg):
    token, chat = _get_telegram_creds()
    if not token:
        return
    try:
        import requests
        requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                      data={'chat_id': chat, 'text': msg}, timeout=10)
    except Exception:
        pass


def _send_telegram_doc(fpath, caption=''):
    token, chat = _get_telegram_creds()
    if not token:
        return
    try:
        import requests
        with open(fpath, 'rb') as fh:
            requests.post(f'https://api.telegram.org/bot{token}/sendDocument',
                          data={'chat_id': chat, 'caption': caption},
                          files={'document': fh}, timeout=30)
    except Exception:
        pass


def generate_equity_html(final, capital, start_date, end_date, labels):
    try:
        import plotly.graph_objects as go
    except ImportError:
        print(f'  {Y}plotly nicht installiert — Chart übersprungen.{NC}')
        return None

    eq_df = final.get('equity_curve')
    if eq_df is None or (hasattr(eq_df, 'empty') and eq_df.empty):
        return None

    times = [str(t) for t in eq_df['timestamp']]
    vals  = [float(v) for v in eq_df['equity']]
    pnl   = final.get('total_pnl_pct', 0)
    dd    = final.get('max_drawdown_pct', 0)
    wr    = final.get('win_rate', 0)
    n     = final.get('trade_count', 0)
    eq    = final.get('end_capital', vals[-1] if vals else capital)
    sign  = '+' if pnl >= 0 else ''
    title = (f"{BOT_NAME} Portfolio — {', '.join(labels)} | "
             f"PnL: {sign}{pnl:.1f}% | Equity: {eq:.2f} USDT | "
             f"MaxDD: {dd:.1f}% | WR: {wr:.1f}% | {n} Trades")

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=times, y=vals, mode='lines', name='Portfolio Equity',
                             line=dict(color='#2563eb', width=2)))
    fig.add_hline(y=capital,
                  line=dict(color='rgba(100,100,100,0.4)', width=1, dash='dash'),
                  annotation_text=f'Start {capital:.0f} USDT',
                  annotation_position='top left')
    fig.update_layout(title=dict(text=title, font=dict(size=12), x=0.5),
                      height=600, template='plotly_white', hovermode='x unified',
                      xaxis=dict(rangeslider=dict(visible=True)),
                      yaxis=dict(title='Equity (USDT)'))
    outfile = f'/tmp/{BOT_NAME}_portfolio_equity.html'
    fig.write_html(outfile)
    print(f'  {G}✓ Chart erstellt: {outfile}{NC}')
    return outfile


def main() -> int:
    parser = argparse.ArgumentParser(description='ZeroBot Portfolio-Optimizer')
    parser.add_argument('--capital',    type=float, default=None)
    parser.add_argument('--max-dd',     type=float, default=30.0)
    parser.add_argument('--start-date', type=str,   default=None)
    parser.add_argument('--end-date',   type=str,   default=None)
    parser.add_argument('--auto-write', action='store_true')
    parser.add_argument('--replot',     action='store_true')
    args = parser.parse_args()

    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    opt           = settings.get('optimization_settings', {})
    capital       = args.capital or float(opt.get('start_capital', 100))
    max_dd        = args.max_dd
    end_date      = args.end_date   or date.today().strftime('%Y-%m-%d')
    start_date    = args.start_date or (
        date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    max_positions = int(settings.get('live_trading_settings', {}).get('max_open_positions', 10))

    print(f"\n{'─'*72}")
    print(f"{B}  ZeroBot — Portfolio-Optimizer (Renko){NC}")
    print(f"  Kapital: {capital:.0f} USDT | MaxDD <= {max_dd:.0f}% | "
          f"Zeitraum: {start_date} → {end_date}")
    print(f"{'─'*72}\n")

    config_files = _scan_configs()
    if not config_files:
        print(f"{R}  Keine Configs in {CONFIGS_DIR}{NC}")
        print(f"  → Zuerst den Optimizer ausführen!\n")
        return 1

    print(f"  {len(config_files)} Config(s) gefunden.\n")
    strategies_data = _build_strategies_data(config_files, start_date, end_date)
    if not strategies_data:
        print(f"{R}  Keine Daten geladen.{NC}")
        return 1

    from zerobot.analysis.portfolio_optimizer import run_portfolio_optimizer
    result = run_portfolio_optimizer(capital, strategies_data, start_date, end_date, max_dd)

    if not result or not result.get('optimal_portfolio'):
        print(f"{R}  Kein Portfolio erfüllt MaxDD <= {max_dd:.0f}%.{NC}\n")
        return 0

    portfolio_files = result['optimal_portfolio'][:max_positions]
    final           = result.get('final_result') or {}

    print(f"\n{'='*72}")
    print(f"{B}  Optimales Portfolio — {len(portfolio_files)} Strategie(n){NC}\n")
    for fname in portfolio_files:
        sd = strategies_data.get(fname, {})
        print(f"  {G}✓{NC} {sd.get('symbol', fname):<26} / {sd.get('timeframe', ''):<6}")
    if final:
        pnl = final.get('total_pnl_pct', 0)
        print(f"\n  Endkapital: {final.get('end_capital', 0):.2f} USDT  "
              f"| PnL: {pnl:+.1f}%  | MaxDD: {final.get('max_drawdown_pct', 0):.2f}%")
    print(f"{'='*72}\n")

    if args.auto_write:
        _write_to_settings(portfolio_files, strategies_data)
        print(f"{G}✓ settings.json aktualisiert — {len(portfolio_files)} Strategie(n).{NC}\n")

        if final:
            labels = [
                f"{strategies_data.get(f, {}).get('symbol', '?')}/{strategies_data.get(f, {}).get('timeframe', '?')}"
                for f in portfolio_files
            ]
            pnl = final.get('total_pnl_pct', 0)
            dd  = final.get('max_drawdown_pct', 0)
            n   = final.get('trade_count', 0)
            wr  = final.get('win_rate', 0)
            eq  = final.get('end_capital', 0)
            _send_telegram(
                f"{BOT_NAME} Auto-Optimizer\n"
                f"{len(portfolio_files)} Strategien | {n} Trades | WR: {wr:.1f}%\n"
                f"PnL: {pnl:+.1f}% | MaxDD: {dd:.1f}% | Equity: {eq:.2f} USDT\n"
                f"Zeitraum: {start_date} -> {end_date}")
            html = generate_equity_html(final, capital, start_date, end_date, labels)
            if html:
                _send_telegram_doc(html, caption=f'{BOT_NAME} Portfolio-Equity | PnL: {pnl:+.1f}%')
    else:
        try:
            ans = input("  Portfolio in settings.json eintragen? (j/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'
        if ans in ('j', 'ja', 'y', 'yes'):
            _write_to_settings(portfolio_files, strategies_data)
            print(f"{G}✓ settings.json aktualisiert.{NC}\n")
        else:
            print(f"{Y}  settings.json NICHT geändert.{NC}\n")

    return 0


if __name__ == '__main__':
    sys.exit(main())
