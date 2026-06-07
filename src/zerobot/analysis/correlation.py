import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest, load_all_configs

load_configs = load_all_configs

def pearson_corr(x, y):
    x = np.array(x, dtype=float)
    y = np.array(y, dtype=float)
    if len(x) < 2:
        return float('nan')
    mx, my = x.mean(), y.mean()
    num = ((x - mx) * (y - my)).sum()
    den = math.sqrt(((x - mx)**2).sum() * ((y - my)**2).sum())
    return num / den if den > 0 else float('nan')


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


def create_chart(labels, corr_matrix):
    """corr_matrix: 2D list of floats"""
    if not labels or not corr_matrix:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.colors import LinearSegmentedColormap
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    n = len(labels)
    mat = np.array(corr_matrix, dtype=float)

    fig, ax = plt.subplots(figsize=(max(6, n * 1.2), max(5, n * 1.1)))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    # Diverging colormap: red(-1) → white(0) → green(+1)
    cmap = LinearSegmentedColormap.from_list(
        'rg', ['#ef4444', '#1e293b', '#16a34a'], N=256)

    im = ax.imshow(mat, cmap=cmap, vmin=-1, vmax=1, aspect='auto')
    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.ax.tick_params(colors='#94a3b8')

    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    short_labels = [l[:12] for l in labels]
    ax.set_xticklabels(short_labels, color='#94a3b8', rotation=45, ha='right', fontsize=8)
    ax.set_yticklabels(short_labels, color='#94a3b8', fontsize=8)

    for i in range(n):
        for j in range(n):
            val = mat[i, j]
            if not math.isnan(val):
                text_color = 'white' if abs(val) > 0.5 else '#94a3b8'
                ax.text(j, i, f'{val:.2f}', ha='center', va='center',
                        color=text_color, fontsize=8, fontweight='bold')

    ax.set_title('Korrelationsmatrix', color='white', fontsize=12)
    plt.tight_layout()

    path = '/tmp/zerobot_correlation.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'correlation_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Anti-Korrelations-Portfolio Analyse')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 70)
    print("  ANTI-KORRELATIONS-PORTFOLIO ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    if len(configs) == 1:
        print("  Nur eine Config — kein Portfolio-Vergleich moeglich.")
        return

    weekly_series = {}
    labels = []

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            continue

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            continue

        records = []
        for t in trades:
            try:
                ts = pd.to_datetime(t['timestamp'], utc=True)
                records.append({'week': ts.isocalendar()[0] * 100 + ts.isocalendar()[1],
                                'pnl_usd': t['pnl_usd']})
            except Exception:
                pass

        if not records:
            continue

        df = pd.DataFrame(records)
        weekly = df.groupby('week')['pnl_usd'].sum()
        label  = fn.replace('config_', '').replace('.json', '')[:20]
        weekly_series[label] = weekly
        labels.append(label)

    if len(labels) < 2:
        print("  Zu wenige Configs mit Trade-Daten fuer Korrelationsanalyse.")
        return

    all_weeks = sorted(set().union(*[set(s.index) for s in weekly_series.values()]))
    matrix_data = {}
    for label in labels:
        s = weekly_series[label]
        matrix_data[label] = [s.get(w, 0.0) for w in all_weeks]

    col_w = max(len(l) for l in labels) + 2

    print(f"  Korrelationsmatrix (woechentliches PnL)")
    print()
    header = f"  {' ':<{col_w}}"
    for l in labels:
        header += f"  {l[:10]:>12}"
    print(header)
    print(f"  {'─'*70}")

    corr_pairs   = []
    corr_matrix  = []

    for i, l1 in enumerate(labels):
        row_vals = []
        row = f"  {l1:<{col_w}}"
        for j, l2 in enumerate(labels):
            if i == j:
                row += f"  {'1.000':>12}"
                row_vals.append(1.0)
            elif j < i:
                c = pearson_corr(matrix_data[l1], matrix_data[l2])
                row += f"  {c:>12.3f}"
                row_vals.append(c)
                corr_pairs.append((c, l1, l2))
            else:
                c = pearson_corr(matrix_data[l1], matrix_data[l2])
                row += f"  {c:>12.3f}"
                row_vals.append(c)
        print(row)
        corr_matrix.append(row_vals)

    print(f"  {'─'*70}")

    best_corr = None
    if corr_pairs:
        valid_pairs = [(c, l1, l2) for c, l1, l2 in corr_pairs if not math.isnan(c)]
        if valid_pairs:
            best = min(valid_pairs, key=lambda x: x[0])
            best_corr = best[0]
            print(f"\n  Bestes Anti-Korrelations-Paar: {best[1]} + {best[2]}")
            print(f"  Korrelation: {best[0]:.3f}")
            if best[0] < -0.3:
                print("  Bewertung: GUTE Diversifikation (r < -0.3)")
            elif best[0] < 0.3:
                print("  Bewertung: NEUTRALE Korrelation")
            else:
                print("  Bewertung: HOHE Korrelation — wenig Diversifikation")

    print("\n" + "=" * 70)

    chart_path = create_chart(labels, corr_matrix)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            corr_str = f"{best_corr:.3f}" if best_corr is not None else "n/a"
            caption = f"ZeroBot Korrelationsmatrix | {len(labels)} Configs | Min-Korr: {corr_str}"
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
