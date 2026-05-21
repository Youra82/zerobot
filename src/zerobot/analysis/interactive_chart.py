# src/zerobot/analysis/interactive_chart.py
# Interaktiver Candlestick-Chart mit Quantum-State-Trade-Signalen
#
# Panels:
#   1. OHLCV-Candlesticks + Regime-Hintergrund (TREND/REVERTING/NEUTRAL)
#      Entry-Marker (▲ LONG grün / ▼ SHORT orange)
#      Exit-Marker  (● WIN cyan / ✗ LOSS rot / ■ TIMEOUT grau)
#      SL- und TP-Linien pro Trade
#      Equity-Kurve (rechte Y-Achse)
#   2. Volumen
#   3. Hurst-Exponent (H>0.55 Trend, H<0.45 Reverting)
#   4. ApEn — Approximated Entropy (Markt-Unordnung)
#   5. State Score (Signalqualität pro Entry)
#
# Output: HTML-Datei in /tmp/ (öffnet im Browser oder wird per Telegram gesendet)

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
sys.path.append(PROJECT_ROOT)

from zerobot.physics.database import StateDB
from zerobot.physics.hurst import rolling_hurst
from zerobot.physics.entropy import rolling_apen
from zerobot.physics.encoder import HURST_WINDOW, APEN_WINDOW
from zerobot.analysis.backtester import run_backtest

logger = logging.getLogger(__name__)

DB_PATH     = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'quantum.db')
RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')


def _load_backtest_pnl() -> dict:
    """Lädt PnL% aus gespeicherten Backtest-JSONs. Key: (market, base_tf)."""
    pnl_map = {}
    if not os.path.isdir(RESULTS_DIR):
        return pnl_map
    seen = {}
    for fname in os.listdir(RESULTS_DIR):
        if not fname.startswith('backtest_') or not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname)) as f:
                d = json.load(f)
            raw_tf  = d.get('timeframe', '')
            market  = d.get('market', '')
            is_test = raw_tf.endswith('_test')
            base_tf = raw_tf.removesuffix('_test').removesuffix('_train')
            key     = (market, base_tf)
            pnl     = d.get('stats', {}).get('total_pnl_pct', None)
            if key not in seen or (is_test and not seen[key][1]):
                seen[key] = (pnl, is_test)
        except Exception:
            pass
    return {k: v[0] for k, v in seen.items()}


def select_pairs() -> list[tuple[str, str]]:
    """Zeigt alle Pairs aus Backtest-Ergebnissen mit PnL% und lässt Nutzer auswählen."""
    pnl_map = _load_backtest_pnl()
    pairs   = sorted(pnl_map.keys(), key=lambda x: (x[0], x[1]))

    if not pairs:
        print("Keine Backtest-Ergebnisse gefunden. Zuerst Mode 1 ausführen.")
        return []

    w = 70
    print("\n" + "=" * w)
    print("  Verfügbare Pairs:  (PnL = gespeicherter Backtest, TEST-Periode)")
    print("=" * w)
    for i, (sym, tf) in enumerate(pairs, 1):
        pnl = pnl_map.get((sym, tf))
        if pnl and pnl > 0:
            pnl_str = f"  [+{pnl:.1f}%]"
        elif pnl is not None:
            pnl_str = f"  [{pnl:.1f}%]"
        else:
            pnl_str = ""
        safe = sym.replace('/', '').replace(':', '')
        print(f"  {i:2d}) {safe}_{tf}{pnl_str}")
    print("=" * w)

    print("\n  Wähle Pair(s):")
    print("  Einzeln: z.B. '1' oder '5'")
    print("  Mehrfach: z.B. '1,3,5' oder '1 3 5'")
    raw = input("\n  Auswahl: ").strip()

    selected = []
    for token in raw.replace(',', ' ').split():
        try:
            idx = int(token)
            if 1 <= idx <= len(pairs):
                if pairs[idx - 1] not in selected:
                    selected.append(pairs[idx - 1])
        except ValueError:
            pass

    if not selected:
        print("Ungültige Auswahl.")
    return selected


def _compute_physics_panels(df: pd.DataFrame):
    """
    Berechnet Hurst-Exponent und ApEn als Panel-Indikatoren.
    Gibt zurück: (hurst_arr, apen_arr, regimes)
    """
    closes = df['close'].values.astype(float)
    hw = min(HURST_WINDOW, len(closes))
    aw = min(APEN_WINDOW,  len(closes))

    hurst_arr = rolling_hurst(closes, window=hw, multiscale=False)
    apen_arr  = rolling_apen(closes, window=aw)

    # Per-Kerzen-Regime basierend auf Hurst
    regimes = []
    for h in hurst_arr:
        h = float(h)
        if h > 0.55:
            regimes.append('TREND')
        elif h < 0.45:
            regimes.append('REVERTING')
        else:
            regimes.append('NEUTRAL')

    return hurst_arr, apen_arr, regimes


def create_chart(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    trades: list[dict],
    stats: dict,
    start_capital: float,
    risk_pct: float = 1.0,
    rr_ratio: float = 2.0,
):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        logger.error("plotly nicht installiert. Bitte: pip install plotly")
        return None

    hurst_arr, apen_arr, regimes = _compute_physics_panels(df)
    hurst_s = pd.Series(hurst_arr, index=df.index)
    apen_s  = pd.Series(apen_arr,  index=df.index)

    fig = make_subplots(
        rows=5, cols=1,
        shared_xaxes=True,
        specs=[
            [{'secondary_y': True}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
        ],
        vertical_spacing=0.022,
        row_heights=[0.40, 0.10, 0.17, 0.17, 0.16],
        subplot_titles=[
            '',
            'Volumen',
            'Hurst-Exponent  (>0.55 Trend | <0.45 Reverting)',
            'ApEn — Approximated Entropy  (höher = chaotischer)',
            'State Score  (Signalqualität)',
        ],
    )

    # ── Regime-Hintergrund ───────────────────────────────────────────────────
    _regime_fill = {
        'TREND':     'rgba(38,166,154,0.25)',
        'REVERTING': 'rgba(255,167,38,0.22)',
        'NEUTRAL':   None,
    }
    prev_reg, blk_start = None, None
    for ts_idx, reg in zip(df.index, regimes):
        if reg != prev_reg:
            if prev_reg and _regime_fill.get(prev_reg) and blk_start is not None:
                fig.add_vrect(
                    x0=blk_start, x1=ts_idx,
                    fillcolor=_regime_fill[prev_reg],
                    layer='below', line_width=0, row=1, col=1,
                )
            blk_start, prev_reg = ts_idx, reg
    if prev_reg and _regime_fill.get(prev_reg) and blk_start is not None:
        fig.add_vrect(
            x0=blk_start, x1=df.index[-1],
            fillcolor=_regime_fill[prev_reg],
            layer='below', line_width=0, row=1, col=1,
        )

    # ── Panel 1: Candlesticks ────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df['open'], high=df['high'],
        low=df['low'],   close=df['close'],
        name='OHLC',
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
    ), row=1, col=1, secondary_y=False)

    # ── Trade-Marker & SL/TP-Linien ─────────────────────────────────────────
    entry_long_x,  entry_long_y,  entry_long_txt  = [], [], []
    entry_short_x, entry_short_y, entry_short_txt = [], [], []
    exit_win_x,  exit_win_y  = [], []
    exit_loss_x, exit_loss_y = [], []
    exit_to_x,   exit_to_y   = [], []

    for t in trades:
        et  = pd.to_datetime(t['entry_time'])
        xt  = pd.to_datetime(t['exit_time'])
        sid = str(t.get('state_id', ''))[:8]
        wr  = f"{t.get('state_winrate', 0):.1%}"
        sc  = f"{t.get('state_score', 0):.3f}"
        tip = (
            f"State: {sid}<br>Score: {sc} | WR: {wr}<br>"
            f"SL: {t['sl_price']:.4f} | TP: {t['tp_price']:.4f}"
        )

        if t['direction'] == 'LONG':
            entry_long_x.append(et);  entry_long_y.append(t['entry_price'])
            entry_long_txt.append(tip)
        else:
            entry_short_x.append(et); entry_short_y.append(t['entry_price'])
            entry_short_txt.append(tip)

        if t['outcome'] == 'WIN':
            exit_win_x.append(xt);  exit_win_y.append(t['exit_price'])
        elif t['outcome'] == 'LOSS':
            exit_loss_x.append(xt); exit_loss_y.append(t['exit_price'])
        else:
            exit_to_x.append(xt);   exit_to_y.append(t['exit_price'])

        fig.add_shape(
            type='line', x0=et, x1=xt,
            y0=t['sl_price'], y1=t['sl_price'],
            line=dict(color='rgba(239,68,68,0.45)', width=1, dash='dot'),
        )
        fig.add_shape(
            type='line', x0=et, x1=xt,
            y0=t['tp_price'], y1=t['tp_price'],
            line=dict(color='rgba(34,197,94,0.45)', width=1, dash='dot'),
        )

    if entry_long_x:
        fig.add_trace(go.Scatter(
            x=entry_long_x, y=entry_long_y, mode='markers',
            marker=dict(color='#26a69a', symbol='triangle-up', size=14,
                        line=dict(width=1, color='#ffffff')),
            name='Entry Long', text=entry_long_txt,
            hovertemplate='%{text}<extra>Entry Long</extra>',
        ), row=1, col=1, secondary_y=False)

    if entry_short_x:
        fig.add_trace(go.Scatter(
            x=entry_short_x, y=entry_short_y, mode='markers',
            marker=dict(color='#ffa726', symbol='triangle-down', size=14,
                        line=dict(width=1, color='#ffffff')),
            name='Entry Short', text=entry_short_txt,
            hovertemplate='%{text}<extra>Entry Short</extra>',
        ), row=1, col=1, secondary_y=False)

    if exit_win_x:
        fig.add_trace(go.Scatter(
            x=exit_win_x, y=exit_win_y, mode='markers',
            marker=dict(color='#00bcd4', symbol='circle', size=11,
                        line=dict(width=1, color='#ffffff')),
            name='Exit TP ✓',
        ), row=1, col=1, secondary_y=False)

    if exit_loss_x:
        fig.add_trace(go.Scatter(
            x=exit_loss_x, y=exit_loss_y, mode='markers',
            marker=dict(color='#ef5350', symbol='x', size=11,
                        line=dict(width=2, color='#ef5350')),
            name='Exit SL ✗',
        ), row=1, col=1, secondary_y=False)

    if exit_to_x:
        fig.add_trace(go.Scatter(
            x=exit_to_x, y=exit_to_y, mode='markers',
            marker=dict(color='#9e9e9e', symbol='square', size=9),
            name='Exit Timeout',
        ), row=1, col=1, secondary_y=False)

    # Regime-Legende
    for label, color in [('Trend', '#26a69a'), ('Reverting', '#ffa726')]:
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode='markers',
            marker=dict(symbol='square', size=10, color=color),
            name=label, showlegend=True,
        ), row=1, col=1, secondary_y=False)

    # ── Equity-Kurve (rechte Y-Achse) ───────────────────────────────────────
    sorted_trades = sorted(trades, key=lambda t: str(t.get('entry_time', '')))
    equity   = start_capital
    peak     = equity
    chart_dd = 0.0
    eq_times = [df.index[0]]
    eq_vals  = [start_capital]
    wins_vis = 0

    for t in sorted_trades:
        risk_amount = equity * (risk_pct / 100.0)
        outcome     = t.get('outcome', 'LOSS')

        if outcome == 'WIN':
            equity += risk_amount * rr_ratio
            wins_vis += 1
        elif outcome == 'LOSS':
            equity -= risk_amount
        else:
            sl_pct_t = max(t.get('sl_pct', 1.0), 0.01)
            equity  += risk_amount * (t.get('pnl_pct', 0.0) / sl_pct_t)

        if equity > peak:
            peak = equity
        if peak > 0:
            dd_now = (peak - equity) / peak * 100.0
            if dd_now > chart_dd:
                chart_dd = dd_now

        eq_times.append(pd.to_datetime(t['entry_time']))
        eq_vals.append(equity)

    if len(eq_vals) > 1:
        fig.add_trace(go.Scatter(
            x=eq_times, y=eq_vals,
            name='Equity',
            line=dict(color='#5c9bd6', width=1.5),
            hovertemplate='Equity: %{y:.2f} USDT<extra></extra>',
        ), row=1, col=1, secondary_y=True)

    # ── Panel 2: Volumen ─────────────────────────────────────────────────────
    if 'volume' in df.columns:
        vol_colors = ['#26a69a' if c >= o else '#ef5350'
                      for c, o in zip(df['close'], df['open'])]
        fig.add_trace(go.Bar(
            x=df.index, y=df['volume'],
            marker_color=vol_colors,
            name='Volumen', showlegend=False, opacity=0.65,
            hovertemplate='Vol: %{y:,.0f}<extra></extra>',
        ), row=2, col=1)

    # ── Panel 3: Hurst-Exponent ──────────────────────────────────────────────
    # Farblich: > 0.55 grün (Trend), < 0.45 orange (Reverting), Mitte grau
    hurst_colors = []
    for h in hurst_arr:
        h = float(h)
        if h > 0.55:
            hurst_colors.append('#26a69a')
        elif h < 0.45:
            hurst_colors.append('#ffa726')
        else:
            hurst_colors.append('#9e9e9e')

    fig.add_trace(go.Scatter(
        x=df.index, y=hurst_s,
        mode='lines', line=dict(color='#9e9e9e', width=0.5),
        showlegend=False,
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=df.index, y=hurst_s,
        mode='markers',
        marker=dict(color=hurst_colors, size=3),
        name='Hurst', showlegend=False,
        hovertemplate='H: %{y:.3f}<extra></extra>',
    ), row=3, col=1)
    # Schwellen
    fig.add_hline(y=0.55, line_dash='dot',
                  line_color='rgba(38,166,154,0.55)', row=3, col=1)
    fig.add_hline(y=0.50, line_dash='dot',
                  line_color='rgba(158,158,158,0.35)', row=3, col=1)
    fig.add_hline(y=0.45, line_dash='dot',
                  line_color='rgba(255,167,38,0.55)', row=3, col=1)
    # Signal-Punkte auf Hurst
    if trades:
        sig_times = [pd.to_datetime(t['entry_time']) for t in trades]
        sig_h = []
        for ts in sig_times:
            try:
                sig_h.append(float(hurst_s.asof(ts)))
            except Exception:
                sig_h.append(0.5)
        fig.add_trace(go.Scatter(
            x=sig_times, y=sig_h, mode='markers',
            marker=dict(symbol='circle-open', size=9, color='#5c9bd6',
                        line=dict(width=2)),
            showlegend=False,
            hovertemplate='Signal<br>H: %{y:.3f}<extra></extra>',
        ), row=3, col=1)

    # ── Panel 4: ApEn ────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=apen_s,
        mode='lines', line=dict(color='#ce93d8', width=1.5),
        fill='tozeroy', fillcolor='rgba(206,147,216,0.08)',
        name='ApEn', showlegend=False,
        hovertemplate='ApEn: %{y:.3f}<extra></extra>',
    ), row=4, col=1)
    # Schwelle bei 1.5 (max_apen_for_trade default)
    fig.add_hline(y=1.5, line_dash='dot',
                  line_color='rgba(239,68,68,0.45)', row=4, col=1)
    if trades:
        sig_a = []
        for ts in sig_times:
            try:
                sig_a.append(float(apen_s.asof(ts)))
            except Exception:
                sig_a.append(float(apen_s.mean()))
        fig.add_trace(go.Scatter(
            x=sig_times, y=sig_a, mode='markers',
            marker=dict(symbol='circle-open', size=9, color='#ce93d8',
                        line=dict(width=2)),
            showlegend=False,
            hovertemplate='Signal<br>ApEn: %{y:.3f}<extra></extra>',
        ), row=4, col=1)

    # ── Panel 5: State Score ─────────────────────────────────────────────────
    if trades:
        score_times  = [pd.to_datetime(t['entry_time']) for t in trades]
        score_vals   = [t.get('state_score', 0.0) for t in trades]
        score_colors = ['#26a69a' if t['direction'] == 'LONG' else '#ffa726'
                        for t in trades]
        outcome_txt  = [
            f"Score: {t.get('state_score', 0):.4f}<br>"
            f"WR: {t.get('state_winrate', 0):.1%}<br>"
            f"Dir: {t['direction']} | {t['outcome']}"
            for t in trades
        ]
        fig.add_trace(go.Bar(
            x=score_times, y=score_vals,
            marker_color=score_colors, opacity=0.75,
            name='State Score', showlegend=False,
            text=outcome_txt,
            hovertemplate='%{text}<extra></extra>',
        ), row=5, col=1)
        if score_vals:
            fig.add_hline(
                y=float(pd.Series(score_vals).mean()),
                line_dash='dot', line_color='rgba(255,255,255,0.3)',
                row=5, col=1,
            )
    else:
        fig.add_trace(go.Scatter(
            x=df.index, y=[0] * len(df),
            mode='lines', line=dict(color='rgba(0,0,0,0)'),
            showlegend=False,
        ), row=5, col=1)

    # ── Stats ────────────────────────────────────────────────────────────────
    n       = len(sorted_trades)
    wr      = wins_vis / n if n > 0 else 0.0
    pnl_pct = (equity - start_capital) / start_capital * 100.0 if start_capital > 0 else 0.0

    title = (
        f"{symbol} {timeframe} — zerobot Quantum States | "
        f"Trades: {n} | WR: {wr:.1%} | "
        f"PnL: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}% | "
        f"MaxDD: {chart_dd:.1f}%"
    )

    fig.update_layout(
        title=dict(text=title, font=dict(size=13), x=0.5, xanchor='center'),
        height=1050,
        hovermode='x unified',
        template='plotly_dark',
        dragmode='zoom',
        xaxis_rangeslider_visible=False,
        legend=dict(orientation='h', yanchor='bottom', y=1.01,
                    xanchor='center', x=0.5, font=dict(size=11)),
        margin=dict(l=60, r=70, t=80, b=40),
        barmode='overlay',
        yaxis2=dict(title='Equity (USDT)', showgrid=False,
                    tickfont=dict(color='#5c9bd6'),
                    title_font=dict(color='#5c9bd6')),
    )

    fig.update_yaxes(title_text='Preis',   row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text='Vol',     row=2, col=1)
    fig.update_yaxes(title_text='Hurst',   row=3, col=1, range=[0.2, 0.8])
    fig.update_yaxes(title_text='ApEn',    row=4, col=1)
    fig.update_yaxes(title_text='Score',   row=5, col=1)

    for row in range(1, 6):
        fig.update_xaxes(rangeslider_visible=False, row=row, col=1)

    return fig


def run_interactive_chart(settings: dict, secrets: dict):
    from zerobot.utils.exchange import Exchange
    from scan_and_learn import HISTORY_DAYS_MAP, fetch_history

    print("\n" + "=" * 60)
    print("  INTERAKTIVE CHARTS")
    print("=" * 60)

    selected_pairs = select_pairs()
    if not selected_pairs:
        return

    print()
    start_raw = input("Startdatum (JJJJ-MM-TT) [leer=beliebig]: ").strip()
    end_raw   = input("Enddatum   (JJJJ-MM-TT) [leer=heute]: ").strip()

    cap_raw = input("Startkapital in USDT [Standard: 50]: ").strip()
    start_capital = float(cap_raw) if cap_raw.replace('.', '').isdigit() else 50.0

    risk_raw = input("Risiko pro Trade in % [Standard: 1.0]: ").strip()
    try:
        chart_risk_pct = float(risk_raw) if risk_raw else None
    except ValueError:
        chart_risk_pct = None

    tg_raw = input("Per Telegram senden? (j/n) [Standard: n]: ").strip().lower()
    send_tg = tg_raw in ('j', 'y', 'yes')

    accounts = secrets.get('zerobot', [])
    if not accounts:
        print("Kein 'zerobot'-Account in secret.json.")
        return
    exchange = Exchange(accounts[0])

    physics_cfg = settings.get('physics_settings', {})
    risk_cfg    = settings.get('risk_settings', {})
    params = {
        'physics': {
            'min_score':          physics_cfg.get('min_score', 0.08),
            'sequence_lengths':   physics_cfg.get('sequence_lengths', [3, 4, 5]),
            'max_apen_for_trade': physics_cfg.get('max_apen_for_trade', 1.5),
        },
        'risk': {'rr_ratio': risk_cfg.get('rr_ratio', 2.0)},
    }

    generated = []

    for symbol, timeframe in selected_pairs:
        print(f"\n--- {symbol} ({timeframe}) ---")
        history_days = HISTORY_DAYS_MAP.get(timeframe, 730)

        print(f"  Lade {history_days} Tage History...")
        df = fetch_history(exchange, symbol, timeframe, history_days)
        if df is None or df.empty:
            print(f"  Keine Daten — übersprungen.")
            continue
        print(f"  {len(df)} Kerzen geladen.")

        db = StateDB(DB_PATH)
        effective_risk = chart_risk_pct if chart_risk_pct is not None else risk_cfg.get('risk_per_entry_pct', 1.0)
        leverage = int(risk_cfg.get('leverage', 5))

        print("  Führe Backtest durch...")
        results = run_backtest(
            df=df, market=symbol, timeframe=timeframe, db=db,
            params=params, start_capital=start_capital,
            risk_per_trade_pct=effective_risk,
            leverage=leverage,
        )
        db.close()

        trades = results.get('trades', [])
        stats  = results.get('stats', {})
        print(f"  {stats.get('total_trades', 0)} Trades | "
              f"WR: {stats.get('win_rate', 0):.1%} | "
              f"PnL: {stats.get('total_pnl_pct', 0):+.1f}%")

        df_chart     = df.copy()
        trades_chart = trades
        if start_raw:
            try:
                sd = pd.Timestamp(start_raw, tz='UTC')
                df_chart     = df_chart[df_chart.index >= sd]
                trades_chart = [t for t in trades_chart
                                if str(t.get('entry_time', '')) >= start_raw]
            except Exception:
                pass
        if end_raw:
            try:
                ed = pd.Timestamp(end_raw + ' 23:59:59', tz='UTC')
                df_chart     = df_chart[df_chart.index <= ed]
                trades_chart = [t for t in trades_chart
                                if str(t.get('entry_time', '')) <= end_raw + ' 23:59:59']
            except Exception:
                pass

        print("  Erstelle Chart...")
        fig = create_chart(
            symbol, timeframe, df_chart, trades_chart, stats, start_capital,
            risk_pct=effective_risk,
            rr_ratio=risk_cfg.get('rr_ratio', 2.0),
        )
        if fig is None:
            continue

        safe_name   = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
        output_file = f"/tmp/zerobot_{safe_name}.html"
        fig.write_html(output_file)
        print(f"  ✅ Chart gespeichert: {output_file}")
        generated.append((symbol, timeframe, output_file))

    print(f"\n✅ {len(generated)} Chart(s) generiert!")

    if send_tg and generated:
        secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
        bot_token, chat_id = '', ''
        try:
            with open(secret_path) as f:
                sec = json.load(f)
            acc = sec.get('zerobot', [{}])[0]
            bot_token = acc.get('telegram_bot_token', '')
            chat_id   = acc.get('telegram_chat_id', '')
        except Exception:
            pass

        if bot_token and chat_id:
            from zerobot.utils.telegram import send_document
            for sym, tf, path in generated:
                send_document(bot_token, chat_id, path,
                              caption=f"zerobot Chart: {sym} {tf}")
                print(f"  ✅ Telegram: {sym} {tf} gesendet.")
        else:
            print("  Telegram nicht konfiguriert (bot_token/chat_id fehlt).")
