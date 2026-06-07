import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest, load_all_configs

load_configs = load_all_configs

COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed', '#0891b2',
          '#f59e0b', '#ec4899']


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


def compute_calmar(pnl_pct, max_dd_pct):
    return pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct


def create_chart(all_series, capital, n_windows, symbol, tf):
    """
    all_series: list of dicts per config:
        {fn, symbol, tf, equity_ts, equity_vals, window_stats}
        window_stats: list of {label, pnl, dd, calmar}
    """
    if not all_series:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("  matplotlib nicht verfügbar — kein Chart.")
        return None

    # One chart per config, each with 2 panels
    for idx, series in enumerate(all_series):
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 11))
        fig.patch.set_facecolor('#0f172a')
        for ax in (ax1, ax2):
            ax.set_facecolor('#1e293b')
            ax.tick_params(colors='#94a3b8')
            ax.xaxis.label.set_color('#94a3b8')
            ax.yaxis.label.set_color('#94a3b8')
            ax.spines[:].set_color('#334155')
            ax.grid(True, alpha=0.15, color='#475569')

        eq_ts   = series['equity_ts']
        eq_vals = series['equity_vals']
        wstats  = series['window_stats']
        sym     = series['symbol']
        tf_     = series['tf']
        fn_     = series['fn']

        # ── Panel 1: Equity-Kurve (log) ───────────────────────────────────
        ax1.axhline(y=capital, color='#475569', linestyle='--', alpha=0.5, linewidth=0.8)
        ax1.text(eq_ts[0], capital * 1.02, f'Start {capital:.0f} USDT',
                 color='#475569', fontsize=8)

        total_pnl = (eq_vals[-1] / capital - 1) * 100 if capital > 0 else 0
        peak      = capital
        max_dd    = 0.0
        for v in eq_vals:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        calmar_total = compute_calmar(total_pnl, max_dd)

        label = (f"ZeroBot EAR  |  PnL: {total_pnl:+.1f}%  |  "
                 f"DD: {max_dd:.1f}%  |  Calmar: {calmar_total:.1f}")
        ax1.plot(eq_ts, eq_vals, color='#2563eb', linewidth=2,
                 label=label, zorder=3)

        # Shade window boundaries
        win_colors = ['#3b82f620', '#16a34a20', '#dc262620', '#d9770620',
                      '#7c3aed20', '#0891b220', '#f59e0b20', '#ec4899  20']
        if len(eq_ts) > 1:
            total_span = (eq_ts[-1] - eq_ts[0]).total_seconds()
            win_span   = total_span / n_windows
            for w in range(n_windows):
                ws_dt = eq_ts[0] + pd.Timedelta(seconds=w * win_span)
                we_dt = eq_ts[0] + pd.Timedelta(seconds=(w + 1) * win_span)
                we_dt = min(we_dt, eq_ts[-1])
                col   = win_colors[w % len(win_colors)]
                ax1.axvspan(ws_dt, we_dt, alpha=0.08,
                            color=COLORS[w % len(COLORS)], zorder=1)
                ax1.axvline(ws_dt, color='#475569', linewidth=0.6,
                            linestyle='--', alpha=0.5, zorder=2)

        ax1.set_title(
            f'ZeroBot EAR Walk-Forward — {n_windows} Fenster (Out-of-Sample)\n'
            f'{sym} ({tf_})  |  Startkapital: {capital:.0f} USDT',
            color='white', fontsize=12, pad=10
        )
        ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
        ax1.legend(fontsize=9, loc='upper left', framealpha=0.3,
                   facecolor='#1e293b', labelcolor='white')
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right',
                 color='#94a3b8')
        try:
            valid_vals = [v for v in eq_vals if v > 0]
            if valid_vals:
                ax1.set_yscale('log')
                ax1.set_ylim(bottom=max(1, min(valid_vals) * 0.8))
        except Exception:
            pass

        # ── Panel 2: Calmar pro Fenster ───────────────────────────────────
        ax2.set_title('Calmar Score pro Fenster (höher = besser)',
                      color='white', fontsize=11, pad=8)

        bar_labels = [w['label']  for w in wstats]
        bar_calmar = [w['calmar'] for w in wstats]
        bar_cols   = [COLORS[i % len(COLORS)] for i in range(len(wstats))]

        bars = ax2.bar(bar_labels, bar_calmar, color=bar_cols,
                       alpha=0.82, edgecolor='#1e293b', linewidth=1.2)

        y_max = max(bar_calmar) if bar_calmar else 1
        for bar, score, ws in zip(bars, bar_calmar, wstats):
            ax2.text(bar.get_x() + bar.get_width() / 2,
                     bar.get_height() + abs(y_max) * 0.01,
                     f'{score:.1f}', ha='center', va='bottom',
                     color='white', fontsize=11, fontweight='bold')
            ax2.text(bar.get_x() + bar.get_width() / 2,
                     -abs(y_max) * 0.04,
                     f"PnL {ws['pnl']:+.0f}%\nDD {ws['dd']:.0f}%",
                     ha='center', va='top', color='#94a3b8', fontsize=7)

        if bar_calmar:
            best_idx = int(np.argmax(bar_calmar))
            bars[best_idx].set_edgecolor('#fbbf24')
            bars[best_idx].set_linewidth(3)
            ax2.text(bars[best_idx].get_x() + bars[best_idx].get_width() / 2,
                     -abs(y_max) * 0.13, '★ BEST',
                     ha='center', va='top',
                     color='#fbbf24', fontsize=9, fontweight='bold')

        ax2.set_xlabel('Fenster', color='#94a3b8')
        ax2.set_ylabel('Calmar Score', color='#94a3b8')
        ax2.axhline(y=0, color='#475569', linewidth=0.8)

        plt.tight_layout(pad=2.5)

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

    start_dt    = pd.to_datetime(args.start_date, utc=True)
    end_dt      = pd.to_datetime(args.end_date, utc=True)
    total_days  = (end_dt - start_dt).days
    window_days = total_days // args.windows

    windows = []
    for i in range(args.windows):
        ws = start_dt + timedelta(days=i * window_days)
        we = ws + timedelta(days=window_days) if i < args.windows - 1 else end_dt
        windows.append((ws.strftime('%Y-%m-%d'), we.strftime('%Y-%m-%d')))

    print("\n" + "=" * 70)
    print("  WALK-FORWARD OUT-OF-SAMPLE TEST (ZeroBot EAR)")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Fenster: {args.windows}  |  Kapital: {args.capital} USDT")
    print()

    all_series = []

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = cfg.get('strategy', {})
        risk      = cfg.get('risk', {})

        print(f"\n{'─'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"{'─'*70}")
        print(f"  {'Fenster':<10} {'Trades':>8} {'Win%':>8} {'PnL%':>10} {'MaxDD%':>9} {'Calmar':>9}")
        print(f"  {'─'*55}")

        # Compound equity across all windows
        running_capital = args.capital
        equity_ts       = [start_dt]
        equity_vals     = [args.capital]
        window_stats    = []

        for i, (ws, we) in enumerate(windows):
            label = f"F{i+1}/{args.windows}"
            data  = load_data(symbol, timeframe, ws, we)
            if data.empty or len(data) < 10:
                print(f"  {label:<10} {'—':>8} {'—':>8} {'—':>10} {'—':>9} {'—':>9}")
                window_stats.append({'label': label, 'pnl': 0, 'dd': 0, 'calmar': 0})
                continue

            res    = run_backtest(data.copy(), strategy, risk, running_capital,
                                  return_trades=True)
            pnl    = res.get('total_pnl_pct', 0)
            dd     = res.get('max_drawdown_pct', 0) * 100
            wr     = res.get('win_rate', 0)
            tr     = res.get('trades_count', 0)
            calmar = compute_calmar(pnl, dd)

            end_cap = res.get('end_capital', running_capital)

            # Build equity points from trades
            trades = res.get('trades', [])
            cap_running = running_capital
            for t in sorted(trades, key=lambda x: x.get('exit_time', '')):
                pnl_usd = t.get('pnl_usd', 0)
                cap_running += pnl_usd
                try:
                    ts = pd.to_datetime(t.get('exit_time', we), utc=True)
                except Exception:
                    ts = pd.to_datetime(we, utc=True)
                equity_ts.append(ts)
                equity_vals.append(round(cap_running, 4))

            # Ensure window end is recorded
            we_ts = pd.to_datetime(we, utc=True)
            if not equity_ts or equity_ts[-1] < we_ts:
                equity_ts.append(we_ts)
                equity_vals.append(round(end_cap, 4))

            running_capital = end_cap
            window_stats.append({'label': label, 'pnl': pnl, 'dd': dd, 'calmar': calmar})
            col = '\033[0;32m' if pnl >= 0 else '\033[0;31m'
            NC  = '\033[0m'
            print(f"  {label:<10} {tr:>8} {wr:>7.1f}% {col}{pnl:>+9.1f}%{NC} "
                  f"{dd:>8.1f}% {calmar:>9.1f}")

        total_pnl = (running_capital / args.capital - 1) * 100
        best      = max(window_stats, key=lambda w: w['calmar'])
        print(f"\n  Gesamt-PnL: {total_pnl:+.1f}%  |  "
              f"Endkapital: {running_capital:.2f} USDT  |  "
              f"Bestes Fenster: {best['label']} (Calmar {best['calmar']:.1f})")

        all_series.append({
            'fn':          fn,
            'symbol':      symbol,
            'tf':          timeframe,
            'equity_ts':   equity_ts,
            'equity_vals': equity_vals,
            'window_stats': window_stats,
        })

    print("\n" + "=" * 70)

    if not all_series:
        return

    chart_path = create_chart(all_series, args.capital, args.windows,
                              all_series[0]['symbol'], all_series[0]['tf'])
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            s   = all_series[0]
            sym = s['symbol']
            tf  = s['tf']
            best = max(s['window_stats'], key=lambda w: w['calmar'])
            caption = (f"ZeroBot Walk-Forward | {sym} ({tf}) | "
                       f"{args.windows} Fenster | Bestes: {best['label']} "
                       f"Calmar {best['calmar']:.1f}")
            send_telegram_photo(token, chat_id, chart_path, caption)

if __name__ == '__main__':
    main()
