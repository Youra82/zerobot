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


def create_chart(chart_rows, symbol, tf):
    """chart_rows: list of (win_label, pnl, is_orig_best)"""
    if not chart_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    labels    = [r[0] for r in chart_rows]
    pnls      = [r[1] for r in chart_rows]
    is_bests  = [r[2] for r in chart_rows]
    colors    = ['#16a34a' if p >= 0 else '#ef4444' for p in pnls]

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.grid(True, alpha=0.15, color='#475569')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    bars = ax.bar(labels, pnls, color=colors, edgecolor='#334155', linewidth=0.5)
    ax.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)

    for bar, val, best in zip(bars, pnls, is_bests):
        mark = ' ✓' if best else ' ✗'
        offset = 0.5 if val >= 0 else -1.5
        ax.text(bar.get_x() + bar.get_width()/2, val + offset,
                f'{val:.1f}%{mark}', ha='center',
                va='bottom' if val >= 0 else 'top',
                color='white', fontsize=8)

    ax.set_title(f'Parameter-Stabilität | {symbol} ({tf})', color='white', fontsize=12)
    ax.set_xlabel('Fenster', color='#94a3b8')
    ax.set_ylabel('PnL%', color='#94a3b8')

    from matplotlib.patches import Patch
    legend = ax.legend(handles=[
        Patch(facecolor='#16a34a', label='Positiv'),
        Patch(facecolor='#ef4444', label='Negativ'),
    ], facecolor='#1e293b', labelcolor='white', fontsize=8)
    ax.text(0.98, 0.02, '✓ = Orig.-RR war best  ✗ = nicht optimal',
            transform=ax.transAxes, color='#94a3b8', fontsize=7,
            ha='right', va='bottom')
    plt.tight_layout()

    path = '/tmp/zerobot_param_stability.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'param_stability_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Parameter Stabilitaets-Analyse')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--windows', type=int, default=4)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    start_dt    = pd.to_datetime(args.start_date, utc=True)
    end_dt      = pd.to_datetime(args.end_date, utc=True)
    total_days  = (end_dt - start_dt).days
    window_days = total_days // args.windows

    windows = []
    for i in range(args.windows):
        ws = start_dt + timedelta(days=i * window_days)
        we = ws + timedelta(days=window_days) if i < args.windows - 1 else end_dt
        windows.append((ws.strftime('%Y-%m-%d'), we.strftime('%Y-%m-%d')))

    rr_variations = [0.85, 1.0, 1.15]

    print("\n" + "=" * 75)
    print("  PARAMETER-STABILITAETS-ANALYSE")
    print("=" * 75)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Fenster: {args.windows}  |  RR-Test: original ±15%")
    print()

    first_chart = True
    chart_path = None

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})
        orig_rr   = risk.get('risk_reward_ratio', 2.0)

        print(f"\n{'─'*75}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Original RR: {orig_rr}")
        print()

        header = f"  {'Fenster':<10}"
        for ws, we in windows:
            header += f"  {'W ('+ws[2:7]+')':>12}"
        print(header)

        row_pnl = f"  {'PnL%':<10}"
        window_data_cache = {}
        window_pnls = []

        for ws, we in windows:
            d = load_data(symbol, timeframe, ws, we)
            window_data_cache[(ws, we)] = d
            if d.empty or len(d) < 20:
                row_pnl += f"  {'—':>12}"
                window_pnls.append(None)
                continue
            res = run_backtest(d.copy(), strategy, risk, args.capital)
            pnl = res.get('total_pnl_pct', 0)
            row_pnl += f"  {pnl:>11.1f}%"
            window_pnls.append(pnl)

        print(row_pnl)
        print()

        rr_labels = [f"RR={orig_rr*v:.2f}" for v in rr_variations]
        header2 = f"  {'RR-Stabilitaet':<18}"
        for ws, we in windows:
            header2 += f"  {'Best?':>12}"
        print(header2)
        print(f"  {'─'*70}")

        for var_factor in rr_variations:
            test_rr = orig_rr * var_factor
            row_s   = f"  RR={test_rr:<14.2f}"
            for ws, we in windows:
                d = window_data_cache.get((ws, we), pd.DataFrame())
                if d.empty or len(d) < 20:
                    row_s += f"  {'—':>12}"
                    continue
                best_pnl = -9999
                best_val = None
                for vf in rr_variations:
                    r_mod = dict(risk)
                    r_mod['risk_reward_ratio'] = orig_rr * vf
                    res = run_backtest(d.copy(), strategy, r_mod, args.capital)
                    p   = res.get('total_pnl_pct', -9999)
                    if p > best_pnl:
                        best_pnl = p
                        best_val = orig_rr * vf
                is_best = abs(test_rr - best_val) < 0.001 if best_val is not None else False
                row_s += f"  {'JA ✓':>12}" if is_best else f"  {'nein':>12}"
            print(row_s)

        orig_best_count = 0
        valid_w_count   = 0
        orig_best_per_window = []

        for ws, we in windows:
            d = window_data_cache.get((ws, we), pd.DataFrame())
            if d.empty or len(d) < 20:
                orig_best_per_window.append(None)
                continue
            valid_w_count += 1
            best_pnl = -9999
            best_val = None
            for vf in rr_variations:
                r_mod = dict(risk)
                r_mod['risk_reward_ratio'] = orig_rr * vf
                res = run_backtest(d.copy(), strategy, r_mod, args.capital)
                p   = res.get('total_pnl_pct', -9999)
                if p > best_pnl:
                    best_pnl = p
                    best_val = orig_rr * vf
            is_orig_best = best_val is not None and abs(best_val - orig_rr) < 0.001
            orig_best_per_window.append(is_orig_best)
            if is_orig_best:
                orig_best_count += 1

        if valid_w_count > 0:
            stability = orig_best_count / valid_w_count
            print(f"\n  Stabilitaets-Score: {orig_best_count}/{valid_w_count} Fenster = {stability*100:.0f}%")
            if stability >= 0.75:
                print("  Bewertung: STABIL — Original-RR ist robust")
            elif stability >= 0.5:
                print("  Bewertung: MODERAT — Original-RR haelt meist stand")
            else:
                print("  Bewertung: INSTABIL — Original-RR nicht optimal ueber Fenster")

        # Build chart rows for first config
        if first_chart:
            chart_rows = []
            for i, ((ws, we), pnl, is_best) in enumerate(
                    zip(windows, window_pnls, orig_best_per_window)):
                if pnl is not None and is_best is not None:
                    chart_rows.append((f"F{i+1}/{args.windows}", pnl, is_best))
            chart_path = create_chart(chart_rows, symbol, timeframe)
            first_chart = False

    print("\n" + "=" * 75)

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            fn0, cfg0 = configs[0]
            sym = cfg0['market']['symbol']
            tf  = cfg0['market']['timeframe']
            caption = f"ZeroBot Parameter-Stabilität | {sym} ({tf}) | RR-Robustheit über Fenster"
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
