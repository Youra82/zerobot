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

def analyze_drawdowns(trades, start_capital):
    if not trades:
        return []

    sorted_trades = sorted(trades, key=lambda t: t['timestamp'])
    equity = [start_capital]
    timestamps = [None]
    for t in sorted_trades:
        equity.append(equity[-1] + t['pnl_usd'])
        timestamps.append(t['timestamp'])

    dd_periods = []
    peak_idx   = 0
    peak_val   = equity[0]
    in_dd      = False
    dd_start   = 0

    for i in range(1, len(equity)):
        val = equity[i]
        if val > peak_val:
            if in_dd:
                depth = (peak_val - min(equity[dd_start:i])) / peak_val * 100 if peak_val > 0 else 0
                try:
                    ts_start = pd.to_datetime(timestamps[dd_start], utc=True)
                    ts_end   = pd.to_datetime(timestamps[i], utc=True)
                    if pd.isna(ts_start) or pd.isna(ts_end):
                        dur_days = float('nan')
                    else:
                        dur_days = (ts_end - ts_start).total_seconds() / 86400
                except Exception:
                    dur_days = float('nan')
                trough_idx = dd_start + int(np.argmin(equity[dd_start:i]))
                dd_periods.append({
                    'start':   timestamps[dd_start],
                    'bottom':  timestamps[trough_idx],
                    'end':     timestamps[i],
                    'depth':   depth,
                    'dur_days': dur_days,
                })
                in_dd = False
            peak_val = val
            peak_idx = i
        elif val < peak_val and not in_dd:
            in_dd    = True
            dd_start = peak_idx

    if in_dd and len(equity) > dd_start:
        depth = (peak_val - min(equity[dd_start:])) / peak_val * 100 if peak_val > 0 else 0
        try:
            ts_start = pd.to_datetime(timestamps[dd_start], utc=True)
            ts_end   = pd.to_datetime(timestamps[-1], utc=True)
            if pd.isna(ts_start) or pd.isna(ts_end):
                dur_days = float('nan')
            else:
                dur_days = (ts_end - ts_start).total_seconds() / 86400
        except Exception:
            dur_days = float('nan')
        trough_idx = dd_start + int(np.argmin(equity[dd_start:]))
        dd_periods.append({
            'start':   timestamps[dd_start],
            'bottom':  timestamps[trough_idx],
            'end':     None,
            'depth':   depth,
            'dur_days': dur_days,
        })

    return dd_periods


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


def create_chart(all_dd_entries):
    """all_dd_entries: list of dicts with keys depth, dur_days, label, is_open"""
    if not all_dd_entries:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.cm as cm
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    # Separate closed (known duration) from open
    closed = [e for e in all_dd_entries if not (e['dur_days'] != e['dur_days'])]
    all_durs = np.array([e['dur_days'] for e in closed], dtype=float)
    all_deps = np.array([e['depth']    for e in closed], dtype=float)
    open_dots = [e for e in all_dd_entries
                 if not (e['dur_days'] != e['dur_days']) and e['is_open']]

    if len(closed) == 0:
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    fig.patch.set_facecolor('#0f172a')
    for ax in [ax1, ax2]:
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        for spine in ax.spines.values():
            spine.set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.title.set_color('white')

    # Left: scatter — closed in red, open in orange
    closed_no_open = [e for e in closed if not e['is_open']]
    if closed_no_open:
        ax1.scatter([e['depth'] for e in closed_no_open],
                    [e['dur_days'] for e in closed_no_open],
                    color='#ef4444', alpha=0.75, edgecolors='#334155',
                    linewidths=0.5, s=55, label=f'Abgeschlossen ({len(closed_no_open)})')
    if open_dots:
        ax1.scatter([e['depth'] for e in open_dots],
                    [e['dur_days'] for e in open_dots],
                    color='#f59e0b', alpha=0.85, edgecolors='#334155',
                    linewidths=0.5, s=70, marker='^',
                    label=f'Noch offen ({len(open_dots)})')

    if len(all_durs) >= 3:
        try:
            z = np.polyfit(all_deps, all_durs, 1)
            p = np.poly1d(z)
            x_line = np.linspace(all_deps.min(), all_deps.max(), 50)
            ax1.plot(x_line, p(x_line), color='#94a3b8', linestyle='--',
                     linewidth=1.0, alpha=0.6, label='Trend')
        except Exception:
            pass

    ax1.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)
    ax1.set_xlabel('DD Tiefe %', color='#94a3b8')
    ax1.set_ylabel('Dauer (Tage)', color='#94a3b8')
    ax1.set_title(f'DD Tiefe vs Dauer — alle Configs ({len(closed)} Perioden)', color='white', fontsize=10)

    # Right: histogram of all durations (closed + open = min-duration)
    bins = max(1, min(20, len(all_durs)))
    ax2.hist(all_durs, bins=bins, color='#ef4444', edgecolor='#1e293b',
             linewidth=0.3, alpha=0.85, label='Alle')
    median_dur = float(np.median(all_durs))
    ax2.axvline(median_dur, color='#f59e0b', linestyle='--', linewidth=1.4,
                label=f'Median: {median_dur:.0f}d')
    ax2.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)
    ax2.set_xlabel('Dauer (Tage)', color='#94a3b8')
    ax2.set_ylabel('Häufigkeit', color='#94a3b8')
    ax2.set_title('Drawdown-Dauer Verteilung (alle Configs)', color='white', fontsize=10)

    fig.suptitle('ZeroBot Drawdown Duration — Alle Strategien kombiniert', color='white', fontsize=12)
    plt.tight_layout()

    path = '/tmp/zerobot_drawdown_duration.png'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'drawdown_duration_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Drawdown Duration Analysis')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 80)
    print("  DRAWDOWN DURATION ANALYSE")
    print("=" * 80)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    all_combined = []  # all dd entries across configs for combined chart

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 20:
            print(f"\n  {fn}: Keine Daten.")
            continue

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            print(f"\n  {fn}: Keine Trades.")
            continue

        dd_periods = analyze_drawdowns(trades, args.capital)

        print(f"\n{'─'*80}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Trades: {res['trades_count']} | PnL: {res['total_pnl_pct']:.1f}% | Max DD: {res['max_drawdown_pct']*100:.1f}%")
        print()

        if not dd_periods:
            print("  Keine Drawdown-Perioden gefunden (nur Gewinne).")
            continue

        print(f"  {'#':<4} {'Start':<12} {'Tief':<12} {'Ende':<12} {'Tiefe%':>8} {'Dauer(T)':>10}")
        print(f"  {'─'*65}")
        for i, dd in enumerate(dd_periods[:10], 1):
            start_str  = str(dd['start'])[:10] if dd['start'] else '—'
            bottom_str = str(dd['bottom'])[:10] if dd['bottom'] else '—'
            end_str    = str(dd['end'])[:10] if dd['end'] else 'offen'
            dur_str    = f"{dd['dur_days']:>9.1f}" if not (dd['dur_days'] != dd['dur_days']) else '        —'
            print(f"  {i:<4} {start_str:<12} {bottom_str:<12} {end_str:<12} {dd['depth']:>7.1f}% {dur_str}")

        if len(dd_periods) > 10:
            print(f"  ... und {len(dd_periods)-10} weitere Drawdown-Perioden")

        durations = [dd['dur_days'] for dd in dd_periods]
        depths    = [dd['depth']    for dd in dd_periods]

        dur_arr    = np.array(durations, dtype=float)
        valid_durs = dur_arr[~np.isnan(dur_arr)]
        n_unknown  = int(np.sum(np.isnan(dur_arr)))
        n_open_dd  = sum(1 for dd in dd_periods if dd['end'] is None and not (dd['dur_days'] != dd['dur_days']))
        n_closed   = len(dd_periods) - n_unknown - n_open_dd

        print(f"\n  Statistik ({len(dd_periods)} Perioden: {n_closed} abgeschlossen, {n_open_dd} offen, {n_unknown} unbekannter Start):")
        if len(valid_durs) > 0:
            open_note = "  (inkl. laufender Drawdowns)" if n_open_dd > 0 else ""
            print(f"    Avg Dauer:       {np.mean(valid_durs):>7.1f} Tage{open_note}")
            print(f"    Max Dauer:       {np.max(valid_durs):>7.1f} Tage")
            print(f"    90. Pz Dauer:    {np.percentile(valid_durs, 90):>7.1f} Tage")
        else:
            print(f"    Dauer-Statistik: —  (alle Perioden offen oder Startdatum unbekannt)")
        print(f"    Avg Tiefe:       {np.mean(depths):>7.1f}%")
        print(f"    Max Tiefe:       {np.max(depths):>7.1f}%")

        label = f"{symbol.split('/')[0]} {timeframe}"
        for dd in dd_periods:
            all_combined.append({
                'depth':    dd['depth'],
                'dur_days': dd['dur_days'],
                'label':    label,
                'is_open':  dd['end'] is None,
            })

    print("\n" + "=" * 80)

    chart_path = create_chart(all_combined)

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            n_total = sum(1 for e in all_combined if not (e['dur_days'] != e['dur_days']))
            caption = f"ZeroBot Drawdown Duration — Alle Strategien | {n_total} Perioden kombiniert"
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
