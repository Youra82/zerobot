import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest, load_active_configs

load_configs = load_active_configs


def get_telegram_credentials():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            s = json.load(f)
        tg = s.get('telegram', {})
        return tg.get('bot_token', ''), tg.get('chat_id', '')
    except Exception:
        return None, None


def send_telegram_photo(token, chat_id, path, caption=''):
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',
                          data={'chat_id': chat_id, 'caption': caption},
                          files={'photo': f}, timeout=30)
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


def create_chart(chart_rows, symbol, current_mvr, current_vfe):
    """chart_rows: list of (label, pnl, trade_count, is_current)"""
    if not chart_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    labels   = [r[0] for r in chart_rows]
    pnls     = [r[1] for r in chart_rows]
    counts   = [r[2] for r in chart_rows]
    is_curr  = [r[3] for r in chart_rows]
    colors   = ['#22d3ee' if c else ('#16a34a' if p >= 0 else '#ef4444')
                for p, c in zip(pnls, is_curr)]

    fig, ax1 = plt.subplots(figsize=(11, 5))
    fig.patch.set_facecolor('#0f172a')
    ax1.set_facecolor('#1e293b')
    ax1.tick_params(colors='#94a3b8')
    for spine in ax1.spines.values():
        spine.set_color('#334155')
    ax1.grid(True, alpha=0.15, color='#475569')
    ax1.xaxis.label.set_color('#94a3b8')
    ax1.yaxis.label.set_color('#94a3b8')
    ax1.title.set_color('white')

    x = range(len(labels))
    bars = ax1.bar(x, pnls, color=colors, edgecolor='#334155', linewidth=0.5)
    ax1.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)

    for bar, pnl, cnt in zip(bars, pnls, counts):
        ax1.text(bar.get_x() + bar.get_width()/2,
                 pnl + (0.3 if pnl >= 0 else -0.8),
                 f'{pnl:.1f}%\n(n={cnt})', ha='center',
                 va='bottom' if pnl >= 0 else 'top',
                 color='white', fontsize=7)

    ax1.set_xticks(list(x))
    ax1.set_xticklabels(labels, color='#94a3b8', rotation=20, ha='right', fontsize=8)
    ax1.set_ylabel('PnL%', color='#94a3b8')
    ax1.set_xlabel('Vol-Filter Konfiguration', color='#94a3b8')
    ax1.set_title(f'Volatilitäts-Filter | {symbol}', color='white', fontsize=12)

    from matplotlib.patches import Patch
    legend = ax1.legend(handles=[
        Patch(facecolor='#22d3ee', label='Aktuelle Config'),
        Patch(facecolor='#16a34a', label='Positiv'),
        Patch(facecolor='#ef4444', label='Negativ'),
    ], facecolor='#1e293b', labelcolor='white', fontsize=8)
    plt.tight_layout()

    path = '/tmp/zerobot_vol_filter.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'vol_filter_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Volatilitaets-Filter Optimierung')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
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
    chart_rows = []

    s_no_filter = dict(strategy)
    s_no_filter['vol_filter_enabled'] = False
    res_base = run_backtest(data.copy(), s_no_filter, risk, args.capital)
    pnl_base = res_base.get('total_pnl_pct', 0)
    results_for_mark['disabled'] = pnl_base
    is_current_base = not current_vfe
    print(f"  {'Filter DEAKTIVIERT':<30} {res_base['trades_count']:>8} {res_base['win_rate']:>7.1f}% "
          f"{pnl_base:>9.1f}% {res_base['max_drawdown_pct']*100:>9.1f}%  (Baseline)")
    chart_rows.append(('Off', pnl_base, res_base['trades_count'], is_current_base))

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
        chart_rows.append((str(mvr), pnl, res['trades_count'], is_current))

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

    chart_path = create_chart(chart_rows, symbol, current_mvr, current_vfe)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            best_str = f"Off" if best_label == 'disabled' else f"min_vol_ratio={best_label}"
            caption = (f"ZeroBot Vol-Filter | {symbol} ({timeframe}) | "
                       f"Bester: {best_str} ({best_pnl:.1f}%)")
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
.Value
        if ($line -notmatch 'load_active_configs') {
            $line + ', load_active_configs'
        } else { $line }
    

load_configs = load_active_configs


def get_telegram_credentials():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            s = json.load(f)
        tg = s.get('telegram', {})
        return tg.get('bot_token', ''), tg.get('chat_id', '')
    except Exception:
        return None, None


def send_telegram_photo(token, chat_id, path, caption=''):
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',
                          data={'chat_id': chat_id, 'caption': caption},
                          files={'photo': f}, timeout=30)
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


def create_chart(chart_rows, symbol, current_mvr, current_vfe):
    """chart_rows: list of (label, pnl, trade_count, is_current)"""
    if not chart_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    labels   = [r[0] for r in chart_rows]
    pnls     = [r[1] for r in chart_rows]
    counts   = [r[2] for r in chart_rows]
    is_curr  = [r[3] for r in chart_rows]
    colors   = ['#22d3ee' if c else ('#16a34a' if p >= 0 else '#ef4444')
                for p, c in zip(pnls, is_curr)]

    fig, ax1 = plt.subplots(figsize=(11, 5))
    fig.patch.set_facecolor('#0f172a')
    ax1.set_facecolor('#1e293b')
    ax1.tick_params(colors='#94a3b8')
    for spine in ax1.spines.values():
        spine.set_color('#334155')
    ax1.grid(True, alpha=0.15, color='#475569')
    ax1.xaxis.label.set_color('#94a3b8')
    ax1.yaxis.label.set_color('#94a3b8')
    ax1.title.set_color('white')

    x = range(len(labels))
    bars = ax1.bar(x, pnls, color=colors, edgecolor='#334155', linewidth=0.5)
    ax1.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)

    for bar, pnl, cnt in zip(bars, pnls, counts):
        ax1.text(bar.get_x() + bar.get_width()/2,
                 pnl + (0.3 if pnl >= 0 else -0.8),
                 f'{pnl:.1f}%\n(n={cnt})', ha='center',
                 va='bottom' if pnl >= 0 else 'top',
                 color='white', fontsize=7)

    ax1.set_xticks(list(x))
    ax1.set_xticklabels(labels, color='#94a3b8', rotation=20, ha='right', fontsize=8)
    ax1.set_ylabel('PnL%', color='#94a3b8')
    ax1.set_xlabel('Vol-Filter Konfiguration', color='#94a3b8')
    ax1.set_title(f'Volatilitäts-Filter | {symbol}', color='white', fontsize=12)

    from matplotlib.patches import Patch
    legend = ax1.legend(handles=[
        Patch(facecolor='#22d3ee', label='Aktuelle Config'),
        Patch(facecolor='#16a34a', label='Positiv'),
        Patch(facecolor='#ef4444', label='Negativ'),
    ], facecolor='#1e293b', labelcolor='white', fontsize=8)
    plt.tight_layout()

    path = '/tmp/zerobot_vol_filter.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'vol_filter_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Volatilitaets-Filter Optimierung')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
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
    chart_rows = []

    s_no_filter = dict(strategy)
    s_no_filter['vol_filter_enabled'] = False
    res_base = run_backtest(data.copy(), s_no_filter, risk, args.capital)
    pnl_base = res_base.get('total_pnl_pct', 0)
    results_for_mark['disabled'] = pnl_base
    is_current_base = not current_vfe
    print(f"  {'Filter DEAKTIVIERT':<30} {res_base['trades_count']:>8} {res_base['win_rate']:>7.1f}% "
          f"{pnl_base:>9.1f}% {res_base['max_drawdown_pct']*100:>9.1f}%  (Baseline)")
    chart_rows.append(('Off', pnl_base, res_base['trades_count'], is_current_base))

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
        chart_rows.append((str(mvr), pnl, res['trades_count'], is_current))

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

    chart_path = create_chart(chart_rows, symbol, current_mvr, current_vfe)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            best_str = f"Off" if best_label == 'disabled' else f"min_vol_ratio={best_label}"
            caption = (f"ZeroBot Vol-Filter | {symbol} ({timeframe}) | "
                       f"Bester: {best_str} ({best_pnl:.1f}%)")
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
