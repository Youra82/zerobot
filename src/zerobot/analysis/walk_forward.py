"""
walk_forward.py — Walk-Forward Lookback-Analyse (ZeroBot EAR)

Frage: Wie viele Wochen zurück soll der Auto-Optimizer schauen,
       um das beste Portfolio zu wählen?

Methode: Rolling Walk-Forward (kein Lookahead)
  Für jeden Lookback (1, 2, 4, 8, 12, 26 Wochen):
    Pro Test-Woche:
      In-Sample  → letzte N Wochen: Configs anhand Calmar selektieren
      Out-of-Sample → nächste Woche: selektierte Configs traden, Equity akkumulieren
  Alle Lookbacks auf demselben OOS-Zeitraum (fairer Vergleich).

Ergebnis: Bester Lookback → settings.json "backtest_lookback_weeks"
"""
import os, sys, json, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from zerobot.analysis.backtester import load_data, run_backtest, load_all_configs

load_configs = load_all_configs

LOOKBACK_WINDOWS = [1, 2, 4, 8, 12, 26]  # Wochen
WARMUP_WEEKS    = 16  # Indikator-Warmup: 16×7=112 Daily-Kerzen > 100-Kerzen-Threshold
COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed', '#0891b2']

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


def compute_stats(curve, capital):
    if not curve:
        return 0.0, 0.0, 0.0
    final_eq = curve[-1][1]
    pnl_pct  = (final_eq - capital) / capital * 100.0
    eq_vals  = [capital] + [e[1] for e in curve]
    peak, max_dd = eq_vals[0], 0.0
    for e in eq_vals:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    calmar = compute_calmar(pnl_pct, max_dd)
    return calmar, pnl_pct, max_dd


def preload_data(configs, full_start_str, full_end_str):
    """Lade alle Daten einmalig — kein wiederholter API-Call."""
    cache = {}
    for fn, cfg in configs:
        sym = cfg['market']['symbol']
        tf  = cfg['market']['timeframe']
        key = (sym, tf)
        if key not in cache:
            print(f"  Lade: {sym} ({tf}) ...", end='', flush=True)
            data = load_data(sym, tf, full_start_str, full_end_str)
            if data.empty:
                print(" keine Daten")
                continue
            if not isinstance(data.index, pd.DatetimeIndex):
                data.index = pd.to_datetime(data.index, utc=True)
            elif data.index.tz is None:
                data.index = data.index.tz_localize('UTC')
            cache[key] = data
            print(f" {len(data)} Kerzen")
    return cache


def slice_data(cache, symbol, tf, start_dt, end_dt):
    key  = (symbol, tf)
    data = cache.get(key)
    if data is None or data.empty:
        return pd.DataFrame()
    mask = (data.index >= start_dt) & (data.index < end_dt)
    return data[mask].copy()


def select_portfolio(configs, cache, is_start, is_end, max_dd_pct,
                     min_candles=5, min_trades=2):
    """
    In-Sample Portfolio-Selektion: wählt beste Config pro Symbol anhand Calmar.
    Backtest läuft mit WARMUP_WEEKS Vorlauf (>100 Kerzen auch für Daily-TF),
    Trades werden aber nur ab is_start gezählt (trade_start_date).
    """
    symbol_best = {}
    for fn, cfg in configs:
        sym      = cfg['market']['symbol']
        tf       = cfg['market']['timeframe']
        strategy = cfg.get('strategy', {})
        risk     = cfg.get('risk', {})

        # Prüfe ob tatsächliches IS-Fenster genug Kerzen hat
        is_data_check = slice_data(cache, sym, tf, is_start, is_end)
        if len(is_data_check) < min_candles:
            continue

        # Backtest mit Warmup-Vorlauf (>100 Kerzen für Indikator-Init)
        is_data = slice_data(cache, sym, tf,
                             is_start - timedelta(weeks=WARMUP_WEEKS), is_end)
        try:
            res = run_backtest(is_data, strategy, risk, 100.0, verbose=False,
                               trade_start_date=is_start.isoformat())
        except Exception:
            continue

        pnl    = res.get('total_pnl_pct', 0)
        dd     = res.get('max_drawdown_pct', 0) * 100
        trades = res.get('trades_count', 0)

        if trades < min_trades or pnl <= 0 or dd > max_dd_pct:
            continue

        calmar = compute_calmar(pnl, dd)
        if sym not in symbol_best or calmar > symbol_best[sym]['calmar']:
            symbol_best[sym] = {'fn': fn, 'cfg': cfg, 'sym': sym, 'tf': tf,
                                'calmar': calmar}
    return list(symbol_best.values())


def run_walk_forward(configs, cache, lookback_weeks, week_starts, capital,
                     max_dd_pct, min_trades=2):
    """
    Simuliert wöchentlichen Auto-Optimizer mit N Wochen Lookback.
    Returns: (curve, empty_weeks, total_trades, total_wins)
    curve: list of (week_end_dt, equity, n_portfolio, n_oos_trades)
    """
    equity      = capital
    curve       = []
    empty_weeks = 0
    total_trades = 0
    total_wins   = 0

    for week_start in week_starts:
        is_start = week_start - timedelta(weeks=lookback_weeks)
        oos_end  = week_start + timedelta(weeks=1)

        portfolio = select_portfolio(configs, cache, is_start, week_start,
                                     max_dd_pct, min_candles=20,
                                     min_trades=min_trades)

        if not portfolio:
            empty_weeks += 1
            curve.append((oos_end, equity, 0, 0))
            continue

        n        = len(portfolio)
        cap_each = equity / n
        oos_pnl  = 0.0
        wk_trades = 0
        wk_wins   = 0

        for item in portfolio:
            # OOS mit Warmup-Vorlauf (>100 Kerzen), Trades nur ab week_start
            oos_data = slice_data(cache, item['sym'], item['tf'],
                                  week_start - timedelta(weeks=WARMUP_WEEKS), oos_end)
            if len(oos_data) < 2:
                continue
            strategy = item['cfg'].get('strategy', {})
            risk     = item['cfg'].get('risk', {})
            try:
                res = run_backtest(oos_data, strategy, risk, cap_each,
                                   verbose=False, return_trades=True,
                                   trade_start_date=week_start.isoformat())
            except Exception:
                continue
            oos_pnl  += res.get('end_capital', cap_each) - cap_each
            wk_trades += res.get('trades_count', 0)
            wk_wins   += sum(1 for t in res.get('trades', []) if t.get('win', False))

        equity  = max(equity + oos_pnl, 0.0)
        curve.append((oos_end, equity, n, wk_trades))
        total_trades += wk_trades
        total_wins   += wk_wins

    return curve, empty_weeks, total_trades, total_wins


def create_chart(results, week_starts, capital):
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

    # ── Panel 1: Equity-Kurven ─────────────────────────────────────────────
    ax1.axhline(y=capital, color='#475569', linestyle='--', alpha=0.5, linewidth=0.8)
    if week_starts:
        ax1.text(week_starts[0], capital * 1.02, f'Start {capital:.0f} USDT',
                 color='#475569', fontsize=8)

    bar_labels, bar_calmar, bar_colors = [], [], []
    best_calmar = -999.0

    for i, (weeks, (curve, empty_w, n_trades, n_wins)) in enumerate(
            sorted(results.items())):
        calmar, pnl_pct, max_dd = compute_stats(curve, capital)
        wr = n_wins / n_trades * 100 if n_trades > 0 else 0.0

        dates    = ([week_starts[0]] if week_starts else []) + [e[0] for e in curve]
        equities = [capital] + [e[1] for e in curve]

        color = COLORS[i % len(COLORS)]
        label = f"{weeks:2}W Lookback: {pnl_pct:+.0f}% | DD {max_dd:.1f}% | Calmar {calmar:.1f}"
        ax1.plot(dates, equities, color=color, linewidth=2, label=label, zorder=3)

        bar_labels.append(f'{weeks}W')
        bar_calmar.append(calmar)
        bar_colors.append(color)
        if calmar > best_calmar:
            best_calmar = calmar

    n_test_weeks = len(week_starts) if week_starts else 0
    ax1.set_title(
        f'ZeroBot EAR Walk-Forward — Lookback-Vergleich (Out-of-Sample)\n'
        f'Startkapital: {capital:.0f} USDT  |  Test-Wochen: {n_test_weeks}',
        color='white', fontsize=12, pad=10
    )
    ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
    ax1.legend(fontsize=9, loc='upper left', framealpha=0.3,
               facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right', color='#94a3b8')
    try:
        all_eq = [capital] + [e[1] for _, (c, *_) in results.items() for e in c]
        min_eq = min(e for e in all_eq if e > 0)
        ax1.set_yscale('log')
        ax1.set_ylim(bottom=max(1, min_eq * 0.5))
    except Exception:
        pass

    # ── Panel 2: Calmar-Balken ─────────────────────────────────────────────
    ax2.set_title('Calmar Score pro Lookback (Out-of-Sample, höher = besser)',
                  color='white', fontsize=11, pad=8)

    bars = ax2.bar(bar_labels, bar_calmar, color=bar_colors,
                   alpha=0.82, edgecolor='#1e293b', linewidth=1.2)

    y_max = max(bar_calmar) if bar_calmar else 1
    for bar, score in zip(bars, bar_calmar):
        ax2.text(bar.get_x() + bar.get_width() / 2,
                 bar.get_height() + abs(y_max) * 0.01,
                 f'{score:.1f}', ha='center', va='bottom',
                 color='white', fontsize=11, fontweight='bold')

    if bar_calmar:
        best_idx = int(np.argmax(bar_calmar))
        bars[best_idx].set_edgecolor('#fbbf24')
        bars[best_idx].set_linewidth(3)
        ax2.text(bars[best_idx].get_x() + bars[best_idx].get_width() / 2,
                 -abs(y_max) * 0.08, '★ BEST',
                 ha='center', va='top',
                 color='#fbbf24', fontsize=9, fontweight='bold')

    ax2.set_xlabel('Lookback-Zeitraum', color='#94a3b8')
    ax2.set_ylabel('Calmar Score (OOS)', color='#94a3b8')
    ax2.axhline(y=0, color='#475569', linewidth=0.8)

    plt.tight_layout(pad=2.5)

    path      = '/tmp/zerobot_walk_forward.png'
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'walk_forward_latest.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


OOS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')


def detect_dark_period(configs):
    """
    Erkennt den Dark Period (OOS) aus:
    1. Config _meta.oos_start  (vom oos_tester geschrieben)
    2. Fallback: artifacts/results/last_oos_run.json
    3. Fallback: _meta.train_end + 1 Tag
    Bei mehreren Configs: spätestes oos_start (konservativste echte OOS für alle).
    Gibt auch per_config zurück: Liste aller Configs mit ihren individuellen oos_starts.
    """
    per_config = []

    # Quelle 1: Config _meta
    for fn, cfg in configs:
        meta  = cfg.get('_meta', {})
        oos_s = meta.get('oos_start') or meta.get('oos_date')
        sym   = cfg.get('market', {}).get('symbol', fn)
        tf    = cfg.get('market', {}).get('timeframe', '')
        if oos_s:
            per_config.append({
                'label':     f'{sym} {tf}',
                'is_start':  meta.get('train_start', '?'),
                'is_end':    meta.get('train_end',   '?'),
                'oos_start': oos_s,
                'source':    'Config _meta',
            })

    if per_config:
        latest = max(per_config, key=lambda x: x['oos_start'])
        return {
            'is_start':   latest['is_start'],
            'is_end':     latest['is_end'],
            'oos_start':  latest['oos_start'],
            'oos_end':    datetime.now().strftime('%Y-%m-%d'),
            'symbol':     latest['label'],
            'source':     'Config _meta',
            'per_config': per_config,
        }

    # Quelle 2: last_oos_run.json
    try:
        with open(OOS_FILE) as f:
            oos_data = json.load(f)
        oos_s = oos_data.get('oos_start')
        if oos_s:
            return {
                'is_start':   oos_data.get('warmup_start', '?'),
                'is_end':     '(aus last_oos_run.json)',
                'oos_start':  oos_s,
                'oos_end':    oos_data.get('oos_end', datetime.now().strftime('%Y-%m-%d')),
                'symbol':     'alle Configs',
                'source':     'last_oos_run.json',
                'per_config': [],
            }
    except Exception:
        pass

    # Quelle 3: train_end aus Config _meta → nächster Tag = OOS-Start
    for fn, cfg in configs:
        train_end = cfg.get('_meta', {}).get('train_end')
        if train_end:
            try:
                oos_s = (pd.to_datetime(train_end) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
                sym   = cfg.get('market', {}).get('symbol', fn)
                tf    = cfg.get('market', {}).get('timeframe', '')
                return {
                    'is_start':   cfg.get('_meta', {}).get('train_start', '?'),
                    'is_end':     train_end,
                    'oos_start':  oos_s,
                    'oos_end':    datetime.now().strftime('%Y-%m-%d'),
                    'symbol':     f'{sym} {tf}',
                    'source':     '_meta.train_end + 1 Tag',
                    'per_config': [],
                }
            except Exception:
                pass

    return None


def main():
    parser = argparse.ArgumentParser(description='Walk-Forward Lookback-Analyse (ZeroBot EAR)')
    parser.add_argument('--start-date',  default=None,
                        help='OOS-Startdatum (Dark Period). Leer = Auto aus Config-Metadata.')
    parser.add_argument('--end-date',    default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital',     type=float, default=100.0)
    parser.add_argument('--max-dd',      type=float, default=30.0,
                        help='Max Drawdown %% für Portfolio-Selektion [Standard: 30]')
    parser.add_argument('--min-trades',  type=int,   default=5,
                        help='Min. Trades pro Config im IS-Fenster [Standard: 5]')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden. Zuerst run_pipeline.sh ausführen.")
        return

    # ── Dark Period: aus Config-Metadata oder manuell
    dark = detect_dark_period(configs)
    if args.start_date:
        # manuell überschrieben
        oos_start_str = args.start_date
        oos_end_str   = args.end_date
        dark_source   = f'manuell ({oos_start_str})'
    elif dark:
        oos_start_str = dark['oos_start']
        oos_end_str   = args.end_date
        dark_source   = f'Auto aus Config ({dark["symbol"]})'
    else:
        oos_start_str = '2023-01-01'
        oos_end_str   = args.end_date
        dark_source   = 'kein _meta gefunden, Fallback'

    oos_weeks_total = max(0,
        (pd.to_datetime(oos_end_str) - pd.to_datetime(oos_start_str)).days // 7)

    print(f"\n{'=' * 65}")
    print(f"  {B}ZeroBot EAR — Walk-Forward Lookback-Analyse{NC}")
    print(f"{'=' * 65}")
    print(f"  Frage: Wie viele Wochen Lookback für den Auto-Optimizer?")
    print()
    if dark and not args.start_date:
        src        = dark.get('source', '')
        per_config = dark.get('per_config', [])
        print(f"  {G}▶ Dark Period (Pipeline OOS) — auto-erkannt [{src}]:{NC}")
        print(f"  ┌─────────────────────────────────────────────────────────┐")
        print(f"  │  IS  (Training):  {dark['is_start']}  →  {dark['is_end']}")
        print(f"  │  OOS (Dark):      {dark['oos_start']}  →  {oos_end_str}  ◄ Walk-Forward")
        print(f"  └─────────────────────────────────────────────────────────┘")

        # Unterschiedliche OOS-Dates pro Config anzeigen
        if len(per_config) > 1:
            dates = sorted(set(c['oos_start'] for c in per_config))
            if len(dates) > 1:
                print(f"  {Y}⚠ Configs haben unterschiedliche OOS-Startdaten:{NC}")
                for c in sorted(per_config, key=lambda x: x['oos_start']):
                    marker = '◄ (gemeinsamer OOS-Start)' if c['oos_start'] == dark['oos_start'] else ''
                    print(f"    {c['label']:<25} OOS ab {c['oos_start']}  {marker}")
                print(f"  {Y}→ Spätestes Datum gewählt: {dark['oos_start']} "
                      f"(konservativste echte OOS für alle Configs){NC}")
            print()
        print(f"  IS-Daten dienen als Lookback-Quelle, OOS wird getestet.")
    else:
        print(f"  Zeitraum:  {oos_start_str} → {oos_end_str}  ({dark_source})")
    print()
    print(f"  OOS-Wochen gesamt:  {oos_weeks_total}W")
    print(f"  Kapital:            {args.capital} USDT  |  Max-DD: {args.max_dd}%")
    print(f"  Configs:            {len(configs)}")
    print(f"  Lookbacks:          {LOOKBACK_WINDOWS} Wochen")

    # Hinweis auf Lookbacks die mehr OOS-Wochen als verfügbar brauchen
    thin = [w for w in LOOKBACK_WINDOWS if oos_weeks_total - w < 4]
    if thin:
        print(f"  {Y}⚠ Lookbacks {thin}W → <4 OOS-Wochen (zu wenig Daten erwartet){NC}")
    print()

    # ── Daten laden (einmalig, für die volle Periode + max. Lookback + Warmup)
    max_lookback = max(LOOKBACK_WINDOWS)
    full_start_dt = (pd.to_datetime(oos_start_str, utc=True)
                     - timedelta(weeks=max_lookback + WARMUP_WEEKS))
    full_end_str  = oos_end_str
    full_start_str = full_start_dt.strftime('%Y-%m-%d')

    print("  Lade Marktdaten...")
    cache = preload_data(configs, full_start_str, full_end_str)
    if not cache:
        print(f"  {R}Keine Daten geladen.{NC}")
        return

    # ── OOS-Zeitraum (alle Lookbacks auf gleichem Zeitraum)
    oos_start_dt = pd.to_datetime(oos_start_str, utc=True)
    oos_end_dt   = pd.to_datetime(oos_end_str, utc=True)

    week_starts = []
    w = oos_start_dt
    while w + timedelta(weeks=1) <= oos_end_dt:
        week_starts.append(w)
        w += timedelta(weeks=1)

    n_weeks = len(week_starts)
    if n_weeks < 4:
        print(f"  {R}Zu wenig Test-Wochen ({n_weeks}). Bitte längeren Zeitraum wählen.{NC}")
        return

    print(f"\n  OOS-Zeitraum: {oos_start_dt.date()} → {oos_end_dt.date()} "
          f"({n_weeks} Test-Wochen)")
    print()

    # ── Walk-Forward für jeden Lookback
    results = {}
    for weeks in LOOKBACK_WINDOWS:
        print(f"  {C}Lookback {weeks:2d}W ...{NC}", end='', flush=True)
        curve, empty_w, n_trades, n_wins = run_walk_forward(
            configs, cache, weeks, week_starts, args.capital, args.max_dd,
            min_trades=args.min_trades)
        calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
        wr = n_wins / n_trades * 100 if n_trades > 0 else 0.0
        results[weeks] = (curve, empty_w, n_trades, n_wins)

        col = G if pnl_pct > 0 else R
        note = '  ← zu wenig Daten' if empty_w > n_weeks * 0.5 else ''
        print(f"  {col}PnL={pnl_pct:+.1f}%{NC} | DD={max_dd:.1f}% | "
              f"Calmar={calmar:.1f} | Trades={n_trades} | "
              f"WR={wr:.1f}% | Leerwochen={empty_w}{note}")

    # ── Bester Lookback (nur Lookbacks mit echten Trades berücksichtigen)
    tradeable = {w: v for w, v in results.items() if v[2] > 0}
    if tradeable:
        best_weeks = max(tradeable,
                         key=lambda w: compute_stats(results[w][0], args.capital)[0])
    else:
        best_weeks = None
    bc, bp, bd = compute_stats(results[best_weeks][0], args.capital) if best_weeks else (0, 0, 0)

    print()
    print(f"  {'─' * 55}")
    if best_weeks and bp > 0:
        print(f"  {G}★ Bester Lookback: {best_weeks} Wochen{NC}")
        print(f"  Calmar: {bc:.1f}  |  PnL: {bp:+.1f}%  |  MaxDD: {bd:.1f}%")
    elif best_weeks:
        print(f"  {Y}⚠ Alle Lookbacks negativ. Bestes Ergebnis: {best_weeks}W{NC}")
        print(f"  Calmar: {bc:.1f}  |  PnL: {bp:+.1f}%  |  MaxDD: {bd:.1f}%")
        print(f"  {Y}→ Config zeigt Out-of-Sample Overfitting. Pipeline neu ausführen?{NC}")
    else:
        print(f"  {R}✗ Kein Lookback mit Trades gefunden. Min-Trades zu hoch?{NC}")
        best_weeks = LOOKBACK_WINDOWS[0]
    print(f"  {'─' * 55}")

    # ── Tabelle
    print()
    print(f"  {'Lookback':<10} {'PnL%':>9} {'MaxDD%':>8} {'Calmar':>9} "
          f"{'Trades':>8} {'WR':>7} {'LeerW':>7}")
    print(f"  {'─' * 60}")
    for weeks in LOOKBACK_WINDOWS:
        curve, empty_w, n_trades, n_wins = results[weeks]
        calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
        wr     = n_wins / n_trades * 100 if n_trades > 0 else 0.0
        marker = '★' if best_weeks and weeks == best_weeks else ' '
        col    = G if pnl_pct > 0 else R
        note   = '  ← zu wenig Daten' if empty_w > n_weeks * 0.5 else ''
        print(f"  {marker}{weeks:2d}W       "
              f"{col}{pnl_pct:>+8.1f}%{NC} "
              f"{max_dd:>7.1f}% "
              f"{calmar:>9.1f} "
              f"{n_trades:>8} "
              f"{wr:>6.1f}% "
              f"{empty_w:>7}{note}")
    print(f"  {'─' * 60}")

    # ── Empfehlung in settings.json schreiben (nur wenn OOS positiv)
    print()
    if bp > 0:
        rec_date = (pd.Timestamp.now(tz='UTC') - timedelta(weeks=best_weeks)).strftime('%Y-%m-%d')
        print(f"  {Y}Empfehlung für settings.json:{NC}")
        print(f'  "backtest_lookback_weeks": {best_weeks}')
        print(f'  (= {best_weeks} Wochen rollierender Lookback, Start: {rec_date})')
        try:
            with open(SETTINGS_PATH) as f:
                settings = json.load(f)
            opt = settings.setdefault('optimization_settings', {})
            opt['backtest_lookback_weeks'] = best_weeks
            with open(SETTINGS_PATH, 'w') as f:
                json.dump(settings, f, indent=4)
            print(f"  {G}✓ settings.json aktualisiert (backtest_lookback_weeks={best_weeks}){NC}")
        except Exception as e:
            print(f"  {Y}settings.json konnte nicht aktualisiert werden: {e}{NC}")
    else:
        print(f"  {Y}settings.json NICHT aktualisiert — OOS-Performance negativ.{NC}")
        print(f"  {Y}Empfehlung: run_pipeline.sh neu ausführen (größerer Zeitraum / andere Config).{NC}")

    # ── Chart
    print()
    chart_path = create_chart(results, week_starts, args.capital)
    if chart_path:
        print(f"  {G}✓ Chart gespeichert: {chart_path}{NC}")

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption_lines = [
                "ZeroBot EAR Walk-Forward — Lookback-Analyse (Out-of-Sample)",
                f"Zeitraum: {oos_start_dt.date()} → {oos_end_dt.date()} ({n_weeks} Wochen)",
                f"Startkapital: {args.capital:.0f} USDT  |  Max-DD: {args.max_dd}%",
                "",
            ]
            for weeks in LOOKBACK_WINDOWS:
                curve, empty_w, n_trades, n_wins = results[weeks]
                calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
                marker = "★ " if weeks == best_weeks else "  "
                caption_lines.append(
                    f"{marker}{weeks:2d}W: {pnl_pct:+.1f}% | "
                    f"DD {max_dd:.1f}% | Calmar {calmar:.1f} | "
                    f"Leerwochen={empty_w}")
            caption_lines.append("")
            if best_weeks and bp > 0:
                caption_lines.append(f"★ Bester Lookback: {best_weeks} Wochen (Calmar {bc:.1f})")
            elif best_weeks:
                caption_lines.append(f"⚠ Alle Lookbacks negativ. Bestes: {best_weeks}W (Calmar {bc:.1f})")
                caption_lines.append("→ Config zeigt OOS-Overfitting. Pipeline neu?")
            send_telegram_photo(token, chat_id, chart_path,
                                "\n".join(caption_lines))
            print(f"  {G}✓ Telegram gesendet.{NC}")

    print(f"\n  {G}Walk-Forward Analyse abgeschlossen.{NC}\n")


if __name__ == '__main__':
    main()
