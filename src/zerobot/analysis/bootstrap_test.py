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


def create_chart(chart_rows, alpha):
    """chart_rows: list of (label, p_value, is_significant)"""
    if not chart_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    labels  = [r[0] for r in chart_rows]
    pvalues = [r[1] for r in chart_rows]
    sigs    = [r[2] for r in chart_rows]
    colors  = ['#16a34a' if s else '#ef4444' for s in sigs]

    fig_height = max(4, len(labels) * 0.5 + 1)
    fig, ax = plt.subplots(figsize=(9, fig_height))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.grid(True, alpha=0.15, color='#475569')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    y_pos = range(len(labels))
    bars = ax.barh(list(y_pos), pvalues, color=colors, edgecolor='#334155', linewidth=0.5)
    ax.axvline(alpha, color='#f59e0b', linestyle='--', linewidth=1.5,
               label=f'Alpha = {alpha}')
    ax.set_xlim(0, 1.0)
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(labels, color='#94a3b8', fontsize=8)
    ax.set_xlabel('p-Wert', color='#94a3b8')
    ax.set_title('Bootstrap Signifikanztest', color='white', fontsize=12)

    for bar, pval in zip(bars, pvalues):
        ax.text(min(pval + 0.02, 0.95), bar.get_y() + bar.get_height() / 2,
                f'{pval:.4f}', va='center', color='white', fontsize=7)

    legend = ax.legend(facecolor='#1e293b', labelcolor='white', fontsize=9)
    plt.tight_layout()

    path = '/tmp/zerobot_bootstrap.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'bootstrap_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Bootstrap Signifikanztest')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--min-trades', type=int, default=10)
    parser.add_argument('--alpha', type=float, default=0.05)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 75)
    print("  BOOTSTRAP SIGNIFIKANZTEST (Binomial, einseitig)")
    print("=" * 75)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Min. Trades: {args.min_trades}  |  Alpha: {args.alpha}")
    print(f"\n  H0: Win-Rate = 50% (Zufall)")
    print(f"  H1: Win-Rate > 50% (Edge vorhanden)")
    print()
    print(f"  {'Config':<40} {'Trades':>7} {'WR%':>7} {'Z-Score':>9} {'p-Wert':>9}  Signifikant")
    print(f"  {'─'*75}")

    significant_count = 0
    total_count       = 0
    chart_rows        = []

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            print(f"  {fn:<40} {'—':>7} {'—':>7} {'—':>9} {'—':>9}  n/a (keine Daten)")
            continue

        res    = run_backtest(data.copy(), strategy, risk)
        trades = res.get('trades_count', 0)
        wr_pct = res.get('win_rate', 0)

        if trades < args.min_trades:
            print(f"  {fn:<40} {trades:>7} {wr_pct:>6.1f}% {'—':>9} {'—':>9}  n/a (< {args.min_trades} Trades)")
            continue

        wins    = round(wr_pct / 100 * trades)
        n       = trades
        z       = (wins - n * 0.5) / math.sqrt(n * 0.25) if n > 0 else 0
        p_value = 0.5 * math.erfc(z / math.sqrt(2))

        is_sig  = p_value < args.alpha
        sig_str = "JA  ***" if is_sig else "nein"
        total_count += 1
        if is_sig:
            significant_count += 1

        label = fn.replace('config_', '').replace('.json', '')
        print(f"  {label:<40} {trades:>7} {wr_pct:>6.1f}% {z:>9.3f} {p_value:>9.4f}  {sig_str}")
        chart_rows.append((label[:30], p_value, is_sig))

    print(f"  {'─'*75}")
    print(f"\n  Ergebnis: {significant_count} von {total_count} Configs signifikant (alpha={args.alpha})")
    if total_count > 0:
        pct = significant_count / total_count * 100
        print(f"  ({pct:.0f}% der Configs zeigen statistisch nachweisbaren Edge)")
    print("\n" + "=" * 75)

    chart_path = create_chart(chart_rows, args.alpha)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption = (f"ZeroBot Bootstrap-Test | {significant_count}/{total_count} "
                       f"signifikant (alpha={args.alpha})")
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
