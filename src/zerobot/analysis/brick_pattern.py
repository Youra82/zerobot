import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest, load_all_configs, FINE_TF_MAP, LazyFineData

load_configs = load_all_configs


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


def create_chart(results, trend_values, reversal_values, best_combo, symbol, tf):
    """results: dict {(tmb, rb): pnl}"""
    if not results:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.colors import LinearSegmentedColormap
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    rows = len(reversal_values)
    cols = len(trend_values)
    mat  = np.zeros((rows, cols))
    valid_pnls = [v for v in results.values() if not math.isnan(v)]
    vmin = min(valid_pnls) if valid_pnls else -10
    vmax = max(valid_pnls) if valid_pnls else 10

    for ri, rb in enumerate(reversal_values):
        for ti, tmb in enumerate(trend_values):
            pnl = results.get((tmb, rb), float('nan'))
            mat[ri, ti] = pnl if not math.isnan(pnl) else vmin

    cmap = LinearSegmentedColormap.from_list(
        'rg', ['#ef4444', '#1e293b', '#16a34a'], N=256)

    fig, ax = plt.subplots(figsize=(max(7, cols * 1.5), max(4, rows * 1.3)))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    im = ax.imshow(mat, cmap=cmap, vmin=vmin, vmax=vmax, aspect='auto')
    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.ax.tick_params(colors='#94a3b8')
    cbar.set_label('PnL%', color='#94a3b8')

    ax.set_xticks(range(cols))
    ax.set_yticks(range(rows))
    ax.set_xticklabels([str(v) for v in trend_values], color='#94a3b8')
    ax.set_yticklabels([str(v) for v in reversal_values], color='#94a3b8')
    ax.set_xlabel('trend_min_bricks', color='#94a3b8')
    ax.set_ylabel('reversal_bricks', color='#94a3b8')

    for ri, rb in enumerate(reversal_values):
        for ti, tmb in enumerate(trend_values):
            pnl = results.get((tmb, rb), float('nan'))
            if math.isnan(pnl):
                continue
            is_best = best_combo == (tmb, rb)
            label = f'{pnl:.1f}%'
            if is_best:
                label += ' ★'
            text_color = 'white'
            ax.text(ti, ri, label, ha='center', va='center',
                    color=text_color, fontsize=8,
                    fontweight='bold' if is_best else 'normal')

    ax.set_title(f'Brick-Pattern-Gitter | {symbol} ({tf})', color='white', fontsize=12)
    plt.tight_layout()

    path = '/tmp/zerobot_brick_pattern.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'brick_pattern_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Brick-Pattern Kombinationsanalyse')
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

    current_tmb = strategy.get('trend_min_bricks', 3)
    current_rb  = strategy.get('reversal_bricks', 2)

    trend_values    = [2, 3, 4, 5, 6]
    reversal_values = [1, 2, 3]

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 20:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    fine_tf = FINE_TF_MAP.get(timeframe)
    fine_data = LazyFineData(symbol, fine_tf) if fine_tf else None

    print("\n" + "=" * 70)
    print("  BRICK-PATTERN KOMBINATIONSANALYSE")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Aktuell: trend_min_bricks={current_tmb}, reversal_bricks={current_rb}")
    print(f"\n  ► = aktuelle Config  |  ★ = beste Kombination")
    print()

    results = {}
    best_pnl = -9999
    best_combo = None

    for tmb in trend_values:
        for rb in reversal_values:
            s_mod = dict(strategy)
            s_mod['trend_min_bricks'] = tmb
            s_mod['reversal_bricks']  = rb
            res = run_backtest(data.copy(), s_mod, risk, args.capital, fine_data=fine_data)
            pnl = res.get('total_pnl_pct', -9999)
            results[(tmb, rb)] = pnl
            if pnl > best_pnl:
                best_pnl  = pnl
                best_combo = (tmb, rb)

    header = f"  {'trend_min_bricks →':<20}"
    for tmb in trend_values:
        header += f"  {'tmb='+str(tmb):>10}"
    print(header)
    print(f"  reversal_bricks ↓")
    print(f"  {'─'*70}")

    for rb in reversal_values:
        row = f"  {'rb='+str(rb):<20}"
        for tmb in trend_values:
            pnl    = results.get((tmb, rb), float('nan'))
            is_best    = (tmb, rb) == best_combo
            is_current = (tmb == current_tmb and rb == current_rb)
            if is_best and is_current:
                marker = " ►★"
            elif is_best:
                marker = "  ★"
            elif is_current:
                marker = "  ►"
            else:
                marker = "   "
            if math.isnan(pnl):
                row += f"  {'—':>10}"
            else:
                row += f"  {pnl:>7.1f}%{marker}"
        print(row)

    print(f"  {'─'*70}")
    if best_combo:
        print(f"\n  Beste Kombination: trend_min_bricks={best_combo[0]}, reversal_bricks={best_combo[1]}")
        print(f"  PnL: {best_pnl:.1f}%")
        curr_pnl = results.get((current_tmb, current_rb), float('nan'))
        if not math.isnan(curr_pnl):
            diff = best_pnl - curr_pnl
            print(f"  Aktuelle Config PnL: {curr_pnl:.1f}%  (Delta zum Besten: {diff:+.1f}%)")

    print("\n" + "=" * 70)

    chart_path = create_chart(results, trend_values, reversal_values, best_combo, symbol, timeframe)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            best_str = f"tmb={best_combo[0]},rb={best_combo[1]} ({best_pnl:.1f}%)" if best_combo else ''
            caption = f"ZeroBot Brick-Pattern | {symbol} ({timeframe}) | Bestes: {best_str}"
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
