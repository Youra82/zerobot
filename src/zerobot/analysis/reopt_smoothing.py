"""
reopt_smoothing.py — Reoptimierungs-Snapshot-Glaettung (ZeroBot EAR)

Frage: backtest_lookback_weeks bleibt bei 4 Wochen, das Portfolio wird weiterhin
       nur woechentlich gewechselt ("ein Team fuer eine Woche") — hilft es, die
       Trailing-4-Wochen-Bewertung nicht an einem einzelnen Stichtag zu messen,
       sondern an mehreren Tagen davor zu sampeln und zu mitteln (geglaettetes
       Signal), um staerker auf die aktuelle Volatilitaet zu reagieren?

Methode: Rolling Walk-Forward (kein Lookahead), Trade-Events pro Config einmalig
         gesammelt (collect_strategy_events-Stil), dann für jede Variante per
         Pandas-Fensterung ausgewertet — kein wiederholter Backtest-Lauf.

Varianten:
  baseline        : 1 Snapshot exakt am Wochenstichtag (aktuelles Verhalten)
  smoothed_1d_N   : N taegliche Snapshots vor dem Stichtag, gemittelt
  smoothed_2d_N   : N Snapshots alle 2 Tage vor dem Stichtag, gemittelt

Ergebnis: Chart (Equity-Kurven + Calmar-Balken), optional Telegram-Versand.
"""
import os, sys, json, argparse
import pandas as pd
import numpy as np
from datetime import timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest, load_all_configs, FINE_TF_MAP
from zerobot.analysis.walk_forward import detect_dark_period

LOOKBACK_WEEKS  = 4
HOLD_DAYS       = 7          # Team wechselt weiterhin nur woechentlich
WARMUP_WEEKS    = 16
COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed', '#0891b2', '#eab308']

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
B  = '\033[1;37m'
NC = '\033[0m'

SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')


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


def collect_all_trades(configs, start_date, end_date):
    """Backtestet jede Config einmal ueber die volle Historie, sammelt Trades
    mit kapitalnormiertem pnl_rel (immun gegen Compounding-Verzerrung ueber
    Jahre) — Basis fuer alle nachgelagerten Walk-Forward-Varianten."""
    rows = []
    for fn, cfg in configs:
        sym = cfg['market']['symbol']
        tf  = cfg['market']['timeframe']
        print(f"  Lade & backteste: {sym} ({tf}) ...", end='', flush=True)
        data = load_data(sym, tf, start_date, end_date)
        if data is None or data.empty or len(data) < 100:
            print(" keine Daten")
            continue
        fine_tf = FINE_TF_MAP.get(tf)
        fine_data = None
        if fine_tf:
            try:
                fine_data = load_data(sym, fine_tf, start_date, end_date)
                if fine_data is None or fine_data.empty:
                    fine_data = None
            except Exception:
                fine_data = None
        try:
            res = run_backtest(data.copy(), cfg.get('strategy', {}), cfg.get('risk', {}),
                               start_capital=100.0, return_trades=True, verbose=False,
                               fine_data=fine_data)
        except Exception as e:
            print(f" Fehler: {e}")
            continue
        trades = res.get('trades', [])
        for t in trades:
            capital_before = t['capital_after'] - t['pnl_usd']
            if capital_before <= 0:
                continue
            rows.append({
                'symbol': sym, 'tf': tf,
                'entry_time': pd.to_datetime(t['entry_time'], utc=True),
                'pnl_rel': t['pnl_usd'] / capital_before,
                'win': t['win'],
            })
        print(f" {len(trades)} Trades")
    return pd.DataFrame(rows)


def score_at(sub, anchor, lookback):
    lb = sub[(sub['entry_time'] >= anchor - lookback) & (sub['entry_time'] < anchor)]
    if lb.empty:
        return None
    return lb.groupby('tf')['pnl_rel'].sum()


def simulate(df, symbols, week_starts, capital, sample_step_days=None, num_samples=1):
    """Woechentlicher Team-Wechsel (HOLD_DAYS), Team-Auswahl je Symbol per bester
    (ggf. geglaetteter) Trailing-4-Wochen-Score. Portfolio-Equity gleichmaessig
    auf die gewaehlten Symbole verteilt, wie im echten Auto-Optimizer/Live-Bot."""
    lookback = pd.Timedelta(weeks=LOOKBACK_WEEKS)
    equity = capital
    curve = [(week_starts[0], equity)]
    total_n, total_wins = 0, 0

    for week_start in week_starts:
        week_end = week_start + timedelta(days=HOLD_DAYS)
        picks = {}
        for sym in symbols:
            sub = df[df['symbol'] == sym]
            if sample_step_days is None:
                avg_score = score_at(sub, week_start, lookback)
            else:
                scores = []
                for k in range(num_samples):
                    anchor = week_start - timedelta(days=k * sample_step_days)
                    s = score_at(sub, anchor, lookback)
                    if s is not None:
                        scores.append(s)
                avg_score = pd.concat(scores, axis=1).mean(axis=1) if scores else None
            if avg_score is None or avg_score.empty:
                continue
            picks[sym] = avg_score.idxmax()

        if not picks:
            curve.append((week_end, equity))
            continue

        n = len(picks)
        cap_each = equity / n
        week_pnl = 0.0
        for sym, tf in picks.items():
            sub = df[(df['symbol'] == sym) & (df['tf'] == tf) &
                    (df['entry_time'] >= week_start) & (df['entry_time'] < week_end)]
            if sub.empty:
                continue
            sym_cap = cap_each
            for _, t in sub.sort_values('entry_time').iterrows():
                sym_cap *= (1 + t['pnl_rel'])
                total_n += 1
                total_wins += int(t['win'])
            week_pnl += (sym_cap - cap_each)

        equity = max(equity + week_pnl, 0.0)
        curve.append((week_end, equity))

    wr = (total_wins / total_n * 100) if total_n else 0.0
    return curve, total_n, wr


def compute_stats(curve, capital):
    if not curve:
        return 0.0, 0.0, 0.0
    final_eq = curve[-1][1]
    pnl_pct  = (final_eq - capital) / capital * 100.0
    peak, max_dd = curve[0][1], 0.0
    for _, e in curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100.0)
    return compute_calmar(pnl_pct, max_dd), pnl_pct, max_dd


def create_chart(results, capital, title_suffix):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print(f"  {R}matplotlib nicht installiert.{NC}")
        return None

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 11))
    fig.patch.set_facecolor('#0f172a')
    for ax in (ax1, ax2):
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.spines[:].set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')

    ax1.axhline(y=capital, color='#475569', linestyle='--', alpha=0.5, linewidth=0.8)

    bar_labels, bar_calmar, bar_colors = [], [], []
    for i, (label, (curve, n_trades, wr)) in enumerate(results.items()):
        calmar, pnl_pct, max_dd = compute_stats(curve, capital)
        dates    = [c[0] for c in curve]
        equities = [c[1] for c in curve]
        color = COLORS[i % len(COLORS)]
        ax1.plot(dates, equities, color=color, linewidth=2,
                 label=f"{label}: {pnl_pct:+.0f}% | DD {max_dd:.1f}% | Calmar {calmar:.1f} | WR {wr:.0f}%",
                 zorder=3)
        bar_labels.append(label)
        bar_calmar.append(calmar)
        bar_colors.append(color)

    ax1.set_title(
        f'ZeroBot EAR — Reoptimierungs-Snapshot-Glaettung ({title_suffix})\n'
        f'Startkapital: {capital:.0f} USDT  |  Lookback: {LOOKBACK_WEEKS}W  |  Team-Wechsel: woechentlich',
        color='white', fontsize=12, pad=10)
    ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
    ax1.legend(fontsize=8.5, loc='upper left', framealpha=0.3,
               facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right', color='#94a3b8')
    try:
        all_eq = [e for (c, *_ ) in results.values() for _, e in c]
        min_eq = min(e for e in all_eq if e > 0)
        ax1.set_yscale('log')
        ax1.set_ylim(bottom=max(1, min_eq * 0.5))
    except Exception:
        pass

    ax2.set_title('Calmar Score pro Variante (hoeher = besser)', color='white', fontsize=11, pad=8)
    bars = ax2.bar(bar_labels, bar_calmar, color=bar_colors, alpha=0.82,
                   edgecolor='#1e293b', linewidth=1.2)
    y_max = max(bar_calmar) if bar_calmar else 1
    for bar, score in zip(bars, bar_calmar):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + abs(y_max) * 0.01,
                 f'{score:.1f}', ha='center', va='bottom', color='white', fontsize=10, fontweight='bold')
    if bar_calmar:
        best_idx = int(np.argmax(bar_calmar))
        bars[best_idx].set_edgecolor('#fbbf24')
        bars[best_idx].set_linewidth(3)
        ax2.text(bars[best_idx].get_x() + bars[best_idx].get_width() / 2,
                 -abs(y_max) * 0.08, '★ BEST', ha='center', va='top',
                 color='#fbbf24', fontsize=9, fontweight='bold')
    ax2.set_xlabel('Variante', color='#94a3b8')
    ax2.set_ylabel('Calmar Score', color='#94a3b8')
    ax2.axhline(y=0, color='#475569', linewidth=0.8)
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=20, ha='right', color='#94a3b8')

    plt.tight_layout(pad=2.5)
    import tempfile
    path      = os.path.join(tempfile.gettempdir(), 'zerobot_reopt_smoothing.png')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'reopt_smoothing_latest.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Reoptimierungs-Snapshot-Glaettung (ZeroBot EAR)')
    parser.add_argument('--start-date', default=None,
                        help='Datenladung/Warmup-Start. Leer = automatisch (OOS-Start - Lookback-Puffer).')
    parser.add_argument('--end-date',   default=None)
    parser.add_argument('--capital',    type=float, default=100.0)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()
    end_date = args.end_date or pd.Timestamp.now().strftime('%Y-%m-%d')

    configs = load_all_configs()
    if not configs:
        print("Keine Configs gefunden. Zuerst run_pipeline.sh ausfuehren.")
        return

    print(f"\n{'=' * 65}")
    print(f"  {B}ZeroBot EAR — Reoptimierungs-Snapshot-Glaettung{NC}")
    print(f"{'=' * 65}")
    print(f"  Frage: Hilft geglaettetes Trailing-Signal bei weiterhin")
    print(f"         woechentlichem Team-Wechsel und {LOOKBACK_WEEKS}W Lookback?")
    print()

    # Nur der echte Out-of-Sample "Dark Period" wird SIMULIERT (getradet) —
    # Daten davor dienen nur als Trailing-Lookback-Material, nicht als
    # gezaehlte Performance (sonst testen wir auf dem Trainings-Fenster).
    dark = detect_dark_period(configs)
    if dark:
        oos_start_str = dark['oos_start']
        print(f"  {G}OOS-Start (Dark Period, auto-erkannt): {oos_start_str} [{dark.get('source','')}]{NC}")
    else:
        oos_start_str = '2026-03-01'
        print(f"  {Y}Kein _meta.oos_start gefunden — Fallback OOS-Start: {oos_start_str}{NC}")

    data_start = args.start_date or (
        pd.to_datetime(oos_start_str) - pd.Timedelta(weeks=LOOKBACK_WEEKS + WARMUP_WEEKS)
    ).strftime('%Y-%m-%d')

    print("  Sammle Trade-Historie aller Configs (einmalig)...")
    df = collect_all_trades(configs, data_start, end_date)
    if df.empty:
        print(f"  {R}Keine Trades gesammelt.{NC}")
        return

    symbols = sorted(df['symbol'].unique())
    start = max(pd.to_datetime(oos_start_str, utc=True),
               df['entry_time'].min() + pd.Timedelta(weeks=LOOKBACK_WEEKS))
    end   = df['entry_time'].max()
    week_starts = []
    w = start
    while w + timedelta(days=HOLD_DAYS) <= end:
        week_starts.append(w)
        w += timedelta(days=HOLD_DAYS)

    if len(week_starts) < 4:
        print(f"  {R}Zu wenig Wochen fuer eine Simulation.{NC}")
        return

    variants = {
        'Baseline (1 Snapshot)':        dict(sample_step_days=None, num_samples=1),
        'Glaettung 1d x3':              dict(sample_step_days=1, num_samples=3),
        'Glaettung 1d x7':              dict(sample_step_days=1, num_samples=7),
        'Glaettung 2d x3':              dict(sample_step_days=2, num_samples=3),
        'Glaettung 2d x7':              dict(sample_step_days=2, num_samples=7),
    }

    print(f"\n  Simulationszeitraum: {start.date()} -> {end.date()}  ({len(week_starts)} Wochen)")
    print(f"  {'Variante':<28}{'Trades':>8}{'WinRate':>10}{'PnL%':>10}{'MaxDD%':>9}{'Calmar':>9}")
    print(f"  {'-'*74}")

    results = {}
    for label, kwargs in variants.items():
        curve, n, wr = simulate(df, symbols, week_starts, args.capital, **kwargs)
        results[label] = (curve, n, wr)
        calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
        col = G if pnl_pct > 0 else R
        print(f"  {label:<28}{n:>8}{wr:>9.1f}%{col}{pnl_pct:>+9.1f}%{NC}{max_dd:>8.1f}%{calmar:>9.1f}")

    best_label = max(results, key=lambda k: compute_stats(results[k][0], args.capital)[0])
    best_calmar, best_pnl, best_dd = compute_stats(results[best_label][0], args.capital)
    print(f"  {'-'*74}")
    print(f"  {G}★ Beste Variante: {best_label}  (Calmar {best_calmar:.1f}, PnL {best_pnl:+.1f}%){NC}")
    if best_label == 'Baseline (1 Snapshot)':
        print(f"  {Y}→ Glaettung bringt hier keinen Vorteil — Baseline bleibt am besten.{NC}")

    chart_path = create_chart(results, args.capital,
                              f"{start.date()} bis {end.date()}")
    if chart_path:
        print(f"\n  {G}✓ Chart gespeichert: {chart_path}{NC}")

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption_lines = [
                "ZeroBot EAR — Reoptimierungs-Snapshot-Glaettung",
                f"Zeitraum: {start.date()} -> {end.date()} ({len(week_starts)} Wochen)",
                f"Startkapital: {args.capital:.0f} USDT | Lookback: {LOOKBACK_WEEKS}W | woechentlicher Team-Wechsel",
                "",
            ]
            for label in variants:
                curve, n, wr = results[label]
                calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
                marker = "★ " if label == best_label else "  "
                caption_lines.append(f"{marker}{label}: {pnl_pct:+.1f}% | DD {max_dd:.1f}% | Calmar {calmar:.1f}")
            caption_lines.append("")
            caption_lines.append(f"★ Beste Variante: {best_label}")
            send_telegram_photo(token, chat_id, chart_path, "\n".join(caption_lines))
            print(f"  {G}✓ Telegram gesendet.{NC}")

    print(f"\n  {G}Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
