import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest, load_all_configs

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


def create_chart(all_results, windows):
    """all_results: list of (fn, symbol, tf, pnl_values, window_labels)"""
    if not all_results:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    n = len(all_results)
    fig, axes = plt.subplots(1, n, figsize=(max(8, 5 * n), 5))
    fig.patch.set_facecolor('#0f172a')
    if n == 1:
        axes = [axes]

    for ax, (fn, symbol, tf, pnl_values, win_labels) in zip(axes, all_results):
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        for spine in ax.spines.values():
            spine.set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.title.set_color('white')

        valid = [(lbl, v) for lbl, v in zip(win_labels, pnl_values) if v is not None]
        if not valid:
            ax.text(0.5, 0.5, 'Keine Daten', color='#94a3b8',
                    ha='center', va='center', transform=ax.transAxes)
            ax.set_title(f'{symbol} ({tf})', color='white', fontsize=10)
            continue

        labels = [x[0] for x in valid]
        vals   = [x[1] for x in valid]
        colors = ['#16a34a' if v >= 0 else '#ef4444' for v in vals]
        bars = ax.bar(labels, vals, color=colors, edgecolor='#334155', linewidth=0.5)
        ax.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)

        for bar, val in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    val + (0.5 if val >= 0 else -1.5),
                    f'{val:.1f}%', ha='center', va='bottom' if val >= 0 else 'top',
                    color='white', fontsize=7)

        short_name = fn.replace('config_', '').replace('.json', '')[:20]
        ax.set_title(f'Walk-Forward Out-of-Sample\n{symbol} ({tf})', color='white', fontsize=9)
        ax.set_xlabel('Fenster', color='#94a3b8')
        ax.set_ylabel('PnL%', color='#94a3b8')

    fig.suptitle('Walk-Forward Out-of-Sample Test', color='white', fontsize=12, y=1.02)
    plt.tight_layout()

    path = '/tmp/zerobot_walk_forward.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'walk_forward_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Walk-Forward Out-of-Sample Test')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--windows', type=int, default=4)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden. Zuerst run_pipeline.sh ausfuehren.")
        return

    start_dt = pd.to_datetime(args.start_date, utc=True)
    end_dt   = pd.to_datetime(args.end_date, utc=True)
    total_days = (end_dt - start_dt).days
    window_days = total_days // args.windows

    windows = []
    for i in range(args.windows):
        ws = start_dt + timedelta(days=i * window_days)
        we = ws + timedelta(days=window_days) if i < args.windows - 1 else end_dt
        windows.append((ws.strftime('%Y-%m-%d'), we.strftime('%Y-%m-%d')))

    print("\n" + "=" * 70)
    print("  WALK-FORWARD OUT-OF-SAMPLE TEST")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Fenster: {args.windows}  |  Kapital: {args.capital} USDT")
    print()

    all_results = []

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        print(f"\n{'─'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"{'─'*70}")
        header = f"  {'Fenster':<8}"
        for i in range(args.windows):
            header += f"  {'W'+str(i+1):>10}"
        print(header)

        pnl_values = []
        win_labels = []
        row_dates  = "  Zeitraum"
        row_trades = "  Trades  "
        row_wr     = "  Win%    "
        row_pnl    = "  PnL%    "

        for i, (ws, we) in enumerate(windows):
            win_labels.append(f"F{i+1}/{args.windows}")
            data = load_data(symbol, timeframe, ws, we)
            if data.empty or len(data) < 20:
                row_dates  += f"  {'n/a':>10}"
                row_trades += f"  {'—':>10}"
                row_wr     += f"  {'—':>10}"
                row_pnl    += f"  {'—':>10}"
                pnl_values.append(None)
                continue
            res = run_backtest(data.copy(), strategy, risk, args.capital)
            pnl = res.get('total_pnl_pct', 0)
            pnl_values.append(pnl)
            row_dates  += f"  {(ws[2:7]):>10}"
            row_trades += f"  {res['trades_count']:>10}"
            row_wr     += f"  {res['win_rate']:>9.1f}%"
            row_pnl    += f"  {pnl:>9.1f}%"

        print(row_dates)
        print(row_trades)
        print(row_wr)
        print(row_pnl)

        valid_pnl = [v for v in pnl_values if v is not None]
        consistency = 'moderat'
        if len(valid_pnl) >= 2:
            std = float(np.std(valid_pnl))
            print(f"\n  Konsistenz (StdDev PnL): {std:.1f}%")
            if std < 10:
                consistency = 'robust'
                print("  Bewertung: ROBUST (StdDev < 10%)")
            elif std > 25:
                consistency = 'instabil'
                print("  Bewertung: INSTABIL (StdDev > 25%)")
            else:
                print("  Bewertung: MODERAT (StdDev 10-25%)")
        else:
            print("  Nicht genug Daten fuer Konsistenzanalyse.")

        all_results.append((fn, symbol, timeframe, pnl_values, win_labels))

    print("\n" + "=" * 70)

    chart_path = create_chart(all_results, windows)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            # build caption from first config
            if all_results:
                _, sym, tf, pnls, _ = all_results[0]
                valid = [v for v in pnls if v is not None]
                std = float(np.std(valid)) if len(valid) >= 2 else 0
                con = 'robust' if std < 10 else ('instabil' if std > 25 else 'moderat')
                caption = f"ZeroBot Walk-Forward | {sym} ({tf}) | Konsistenz: {con}"
            else:
                caption = "ZeroBot Walk-Forward Out-of-Sample"
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
