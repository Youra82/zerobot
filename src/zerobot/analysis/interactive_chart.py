# src/zerobot/analysis/interactive_chart.py
"""
ZeroBot Interaktive Charts (Modus 4)

Generiert Plotly-HTML mit:
  - EAR-Brick-Chart (Entropy-Adaptive Renko Bricks, keine OHLCV)
  - Entry/Exit Trade-Marker (Long/Short, TP/SL)
  - Equity-Curve (rechte Y-Achse)
  - Volumen-Panel (pro Brick)
  - ATR-Panel
"""

import os
import sys
import json
import numpy as np
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
CHARTS_DIR  = os.path.join(PROJECT_ROOT, 'artifacts', 'charts')

GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
RED    = '\033[0;31m'
CYAN   = '\033[0;36m'
BOLD   = '\033[1m'
NC     = '\033[0m'


def _load_all_configs() -> list:
    if not os.path.exists(CONFIGS_DIR):
        return []
    files = sorted(f for f in os.listdir(CONFIGS_DIR)
                   if f.startswith('config_') and f.endswith('.json'))
    configs = []
    for fn in files:
        path = os.path.join(CONFIGS_DIR, fn)
        try:
            with open(path) as f:
                cfg = json.load(f)
            cfg['_filename'] = fn
            configs.append(cfg)
        except Exception:
            pass
    return configs


def _build_ear_bricks(df, strategy_params: dict) -> tuple:
    """
    Baut EAR-Bricks (Entropy-Adaptive Renko) aus OHLCV und gibt ein DataFrame
    mit OHLC-Daten pro Brick zurueck (brick_idx als Index).

    Jeder Brick:
      open  = Preis-Level vor dem Brick (= Close des Vorgaenger-Bricks)
      close = Preis-Level nach dem Brick
      high  = max(open, close)
      low   = min(open, close)
      timestamp = Timestamp der ausloesenden Kerze
      direction = +1 (up) / -1 (down)
      volume, atr, H = Werte der ausloesenden Kerze / Entropie

    Gibt (brick_df, avg_brick_size) zurueck.
    """
    import pandas as pd
    from zerobot.strategy.ear_engine import EAREngine

    engine     = EAREngine(settings=strategy_params)
    raw_bricks = engine._build_bricks(df)

    if not raw_bricks:
        return pd.DataFrame(), 0.0

    timestamps = df.index.tolist()
    has_vol    = 'volume' in df.columns
    volumes    = df['volume'].values if has_vol else None

    records = []
    for i, b in enumerate(raw_bricks):
        cidx  = int(b['candle_idx'])
        o     = raw_bricks[i - 1]['close'] if i > 0 else b['close']
        c     = b['close']
        direction = 1 if b['direction'] == 'up' else -1
        ts    = timestamps[cidx] if cidx < len(timestamps) else timestamps[-1]
        vol   = float(volumes[cidx]) if volumes is not None and cidx < len(volumes) else 0.0
        atr_v = float(b['atr']) if not np.isnan(b['atr']) else 0.0
        records.append({
            'timestamp': ts,
            'open':      o,
            'close':     c,
            'high':      max(o, c),
            'low':       min(o, c),
            'direction': direction,
            'volume':    vol,
            'atr':       atr_v,
            'H':         float(b['H']),
        })

    brick_df = pd.DataFrame(records)
    brick_df.index.name = 'brick_idx'
    avg_size = brick_df['close'].diff().abs().median() if len(brick_df) > 1 else 0.0
    return brick_df, float(avg_size)


def _detect_ear_signals(brick_df, strategy_params: dict) -> list:
    """
    Gibt Liste von Brick-Indizes zurueck an denen ein EAR N-Brick-Signal vorlag.
    Format: [(brick_idx, direction), ...] wobei direction = +1 LONG, -1 SHORT

    Signal: trend_min_bricks aufeinanderfolgende Bricks in gleicher Richtung.
    """
    trend_min_bricks = int(strategy_params.get('trend_min_bricks', 3))

    if len(brick_df) < trend_min_bricks:
        return []

    dirs    = brick_df['direction'].values
    signals = []

    for i in range(trend_min_bricks - 1, len(brick_df)):
        window_dirs = dirs[i - trend_min_bricks + 1:i + 1]
        if all(d == 1  for d in window_dirs):
            signals.append((i, 1))
        elif all(d == -1 for d in window_dirs):
            signals.append((i, -1))

    return signals


def _ts_to_brick(brick_df, ts_str: str) -> int:
    """Gibt den naechsten Brick-Index zum gegebenen Timestamp-String zurueck."""
    import pandas as pd
    try:
        ts = pd.to_datetime(ts_str, utc=True)
        brick_ts = pd.to_datetime(brick_df['timestamp'], utc=True, errors='coerce')
        idx = (brick_ts - ts).abs().idxmin()
        return int(idx)
    except Exception:
        return 0


def _generate_chart(symbol: str, timeframe: str,
                    start_date: str, end_date: str,
                    start_capital: float,
                    strategy_params: dict, risk_params: dict,
                    trade_start_date: str = None,
                    warmup_start: str = None) -> str:
    """Generiert HTML-EAR-Chart. Gibt Pfad zur HTML-Datei zurueck."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        import ta
        import pandas as pd
    except ImportError:
        print(f'{RED}Fehler: plotly / ta nicht installiert.{NC}')
        return ''

    from zerobot.analysis.backtester import load_data, run_backtest

    # OOS-Modus: lade Warmup-Daten fuer korrekten Brick-State
    if warmup_start:
        print(f'INFO: OOS-Modus — lade Daten ab Warmup {warmup_start} (Brick-State-Aufbau)...')
        df_full = load_data(symbol, timeframe, warmup_start, end_date)
        if df_full is None or df_full.empty:
            print(f'INFO: {RED}Keine Daten fuer {symbol} ({timeframe}).{NC}')
            return ''
        atr_ind_full = ta.volatility.AverageTrueRange(
            high=df_full['high'], low=df_full['low'], close=df_full['close'], window=14)
        df_full['atr'] = atr_ind_full.average_true_range()
        df_full.dropna(subset=['atr'], inplace=True)
        # Backtest auf vollen Daten, Trades erst ab trade_start_date/start_date
        bt_trade_start = trade_start_date or start_date
        print('INFO: Fuehre OOS-Backtest durch (Warmup kausal, Trades ab ' + bt_trade_start + ')...')
        res = run_backtest(df_full.copy(), strategy_params, risk_params,
                           start_capital=start_capital, return_trades=True,
                           trade_start_date=bt_trade_start)
        # Visueller Datensatz: nur OOS-Periode (bricks werden auf gefilterten Daten neu gebaut)
        ts_start = pd.to_datetime(start_date, utc=True)
        df = df_full[df_full.index >= ts_start].copy()
    else:
        print(f'INFO: Lade OHLCV-Daten fuer {symbol} {timeframe}...')
        df = load_data(symbol, timeframe, start_date, end_date)
        if df is None or df.empty:
            print(f'INFO: {RED}Keine Daten fuer {symbol} ({timeframe}).{NC}')
            return ''
        atr_ind = ta.volatility.AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'], window=14)
        df['atr'] = atr_ind.average_true_range()
        df.dropna(subset=['atr'], inplace=True)
        print('INFO: Fuehre Backtest durch...')
        res = run_backtest(df.copy(), strategy_params, risk_params,
                           start_capital=start_capital, return_trades=True,
                           trade_start_date=trade_start_date)

    trades = res.get('trades', [])

    # EAR-Bricks (visuell) — auf dem angezeigten Zeitraum (df = OOS-Periode oder volle Periode)
    print('INFO: Berechne EAR-Bricks (visuell)...')
    brick_df, brick_size = _build_ear_bricks(df, strategy_params)
    if brick_df.empty:
        print(f'{RED}Keine Bricks berechnet.{NC}')
        return ''

    n_bricks  = len(brick_df)
    x_idx     = list(range(n_bricks))
    n_ticks   = min(20, n_bricks)
    tick_step = max(1, n_bricks // n_ticks)
    tick_vals = list(range(0, n_bricks, tick_step))
    tick_text = [str(brick_df.iloc[i]['timestamp'])[:10] for i in tick_vals]

    signals    = _detect_ear_signals(brick_df, strategy_params)
    long_sigs  = [i for i, d in signals if d ==  1]
    short_sigs = [i for i, d in signals if d == -1]

    pnl_pct  = res.get('total_pnl_pct', 0.0)
    win_rate = res.get('win_rate', 0.0)
    max_dd   = res.get('max_drawdown_pct', 0.0)
    n_trades = res.get('trades_count', 0)

    # Equity-Kurve auf Brick-Index-Basis
    eq_x = [0]
    eq_y = [start_capital]
    for t in trades:
        bx = _ts_to_brick(brick_df, t.get('exit_time', ''))
        eq_x.append(bx)
        eq_y.append(t.get('capital_after', start_capital))

    # Trade-Marker auf Brick-Basis
    long_entries  = [t for t in trades if t.get('side') == 'long']
    short_entries = [t for t in trades if t.get('side') == 'short']
    tp_exits      = [t for t in trades if t.get('win')]
    sl_exits      = [t for t in trades if not t.get('win')]

    def _entry_bx(t):
        return _ts_to_brick(brick_df, t.get('entry_time', ''))

    def _exit_bx(t):
        return _ts_to_brick(brick_df, t.get('exit_time', ''))

    # Farben
    up_color   = '#26a69a'
    down_color = '#ef5350'

    # --- Figur: 3 Panels ---
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        specs=[
            [{'secondary_y': True}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
        ],
        vertical_spacing=0.020,
        row_heights=[0.62, 0.14, 0.24],
        subplot_titles=['', 'Volumen', 'ATR'],
    )

    # --- Panel 1: EAR-Candlestick ---
    renko_colors = [up_color if d == 1 else down_color
                    for d in brick_df['direction']]
    hover_text = [
        f'Brick #{i}<br>Zeit: {str(brick_df.iloc[i]["timestamp"])[:16]}<br>'
        f'Open: {brick_df.iloc[i]["open"]:.4f}<br>Close: {brick_df.iloc[i]["close"]:.4f}<br>'
        f'Dir: {"▲ UP" if brick_df.iloc[i]["direction"] == 1 else "▼ DOWN"}'
        for i in range(n_bricks)
    ]
    fig.add_trace(go.Candlestick(
        x=x_idx,
        open=brick_df['open'],
        high=brick_df['high'],
        low=brick_df['low'],
        close=brick_df['close'],
        name='EAR',
        increasing_line_color=up_color,
        increasing_fillcolor=up_color,
        decreasing_line_color=down_color,
        decreasing_fillcolor=down_color,
        showlegend=True,
        text=hover_text,
        hoverinfo='text',
    ), row=1, col=1, secondary_y=False)

    # EAR LONG-Signale (grüne Rauten unter dem Brick)
    if long_sigs:
        sig_y = [float(brick_df.iloc[i]['low']) * 0.997 for i in long_sigs]
        fig.add_trace(go.Scatter(
            x=long_sigs, y=sig_y,
            mode='markers',
            marker=dict(symbol='diamond', size=9, color='#00e676',
                        line=dict(color='#ffffff', width=0.5)),
            name='EAR LONG-Signal',
            hovertemplate='LONG Signal<br>Brick %{x}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # EAR SHORT-Signale (rote Rauten ueber dem Brick)
    if short_sigs:
        sig_y = [float(brick_df.iloc[i]['high']) * 1.003 for i in short_sigs]
        fig.add_trace(go.Scatter(
            x=short_sigs, y=sig_y,
            mode='markers',
            marker=dict(symbol='diamond', size=9, color='#ff1744',
                        line=dict(color='#ffffff', width=0.5)),
            name='EAR SHORT-Signal',
            hovertemplate='SHORT Signal<br>Brick %{x}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Entry Long (grüne Dreiecke)
    if long_entries:
        bx_list = [_entry_bx(t) for t in long_entries]
        ep_list = [t['entry_price'] for t in long_entries]
        fig.add_trace(go.Scatter(
            x=bx_list, y=ep_list, mode='markers',
            marker=dict(symbol='triangle-up', size=16, color='#26a69a',
                        line=dict(color='#ffffff', width=1)),
            name='Entry Long ▲',
            hovertemplate='Entry Long<br>Brick %{x}<br>Preis: %{y:.4f}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Entry Short (orange Dreiecke)
    if short_entries:
        bx_list = [_entry_bx(t) for t in short_entries]
        ep_list = [t['entry_price'] for t in short_entries]
        fig.add_trace(go.Scatter(
            x=bx_list, y=ep_list, mode='markers',
            marker=dict(symbol='triangle-down', size=16, color='#ffa726',
                        line=dict(color='#ffffff', width=1)),
            name='Entry Short ▼',
            hovertemplate='Entry Short<br>Brick %{x}<br>Preis: %{y:.4f}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Exit TP (cyan Kreise)
    if tp_exits:
        bx_list = [_exit_bx(t) for t in tp_exits]
        ep_list = [t['exit_price'] for t in tp_exits]
        fig.add_trace(go.Scatter(
            x=bx_list, y=ep_list, mode='markers',
            marker=dict(symbol='circle', size=13, color='#00bcd4',
                        line=dict(color='#ffffff', width=1)),
            name='Exit TP ✓',
            hovertemplate='Exit TP<br>Brick %{x}<br>Preis: %{y:.4f}<br>PnL: %{customdata:.4f} USDT<extra></extra>',
            customdata=[t.get('pnl_usd', 0) for t in tp_exits],
        ), row=1, col=1, secondary_y=False)

    # Exit SL (rote ×)
    if sl_exits:
        bx_list = [_exit_bx(t) for t in sl_exits]
        ep_list = [t['exit_price'] for t in sl_exits]
        fig.add_trace(go.Scatter(
            x=bx_list, y=ep_list, mode='markers',
            marker=dict(symbol='x', size=14, color='#ef5350',
                        line=dict(color='#ef5350', width=3)),
            name='Exit SL ✗',
            hovertemplate='Exit SL<br>Brick %{x}<br>Preis: %{y:.4f}<br>PnL: %{customdata:.4f} USDT<extra></extra>',
            customdata=[t.get('pnl_usd', 0) for t in sl_exits],
        ), row=1, col=1, secondary_y=False)

    # SL-Linien pro Trade (horizontal von Entry bis Exit)
    for i, t in enumerate(trades):
        ebx = _entry_bx(t)
        xbx = _exit_bx(t)
        sl  = t.get('stop_loss')
        if sl is None:
            continue
        show_legend = (i == 0)
        # SL-Linie (rot gestrichelt) — immer zeichnen (TP ist Brick-Exit, kein fester Level)
        fig.add_trace(go.Scatter(
            x=[ebx, xbx], y=[sl, sl],
            mode='lines',
            line=dict(color='rgba(239,83,80,0.65)', width=1, dash='dot'),
            name='Stop-Loss',
            legendgroup='sl',
            showlegend=show_legend,
            hovertemplate=f'SL: {sl:.4f}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Equity-Kurve (rechte Y-Achse)
    fig.add_trace(go.Scatter(
        x=eq_x, y=eq_y,
        mode='lines',
        line=dict(color='#5c9bd6', width=1.5),
        name='Equity',
        hovertemplate='Equity: %{y:.2f} USDT<extra></extra>',
    ), row=1, col=1, secondary_y=True)

    # --- Panel 2: Volumen ---
    vol_colors = [up_color if d == 1 else down_color
                  for d in brick_df['direction']]
    fig.add_trace(go.Bar(
        x=x_idx, y=brick_df['volume'],
        marker_color=vol_colors,
        name='Volumen', showlegend=False, opacity=0.65,
    ), row=2, col=1)

    # --- Panel 3: ATR ---
    fig.add_trace(go.Scatter(
        x=x_idx, y=brick_df['atr'],
        mode='lines', line=dict(color='#42a5f5', width=1.3),
        fill='tozeroy', fillcolor='rgba(66,165,245,0.08)',
        name='ATR', showlegend=False,
        hovertemplate='ATR: %{y:.4f}<extra></extra>',
    ), row=3, col=1)

    # ATR MA(20) als gestrichelte Referenzlinie
    import pandas as pd
    atr_series = brick_df['atr']
    atr_ma     = atr_series.rolling(20, min_periods=1).mean()
    fig.add_trace(go.Scatter(
        x=x_idx, y=atr_ma,
        mode='lines', line=dict(color='rgba(255,167,38,0.5)', width=1, dash='dot'),
        showlegend=False,
        hovertemplate='ATR-MA: %{y:.4f}<extra></extra>',
    ), row=3, col=1)

    # Brick-Größe als horizontale Linie auf ATR-Panel
    fig.add_hline(y=brick_size, line_color='rgba(255,255,255,0.25)',
                  line_dash='dash', line_width=1, row=3, col=1,
                  annotation_text=f'Brick={brick_size:.4f}',
                  annotation_font_color='rgba(255,255,255,0.5)',
                  annotation_position='top left')

    # --- Layout ---
    sign       = '+' if pnl_pct >= 0 else ''
    title_text = (
        f'{symbol} {timeframe} — ZeroBot EAR | '
        f'Bricks: {n_bricks} | Brick-Groesse: {brick_size:.4f} | '
        f'Trades: {n_trades} | WR: {win_rate:.1f}% | '
        f'PnL: {sign}{pnl_pct:.1f}% | MaxDD: {max_dd:.1f}%'
    )

    fig.update_layout(
        title=dict(text=title_text, font=dict(size=13), x=0.5, xanchor='center'),
        template='plotly_dark',
        xaxis_rangeslider_visible=False,
        legend=dict(orientation='h', yanchor='bottom', y=1.01,
                    xanchor='center', x=0.5, font=dict(size=11)),
        height=1050,
        margin=dict(l=60, r=80, t=90, b=40),
        yaxis2=dict(title='Equity (USDT)', showgrid=False,
                    tickfont=dict(color='#5c9bd6'),
                    title_font=dict(color='#5c9bd6')),
    )

    # X-Achse: Brick-Index mit Datum-Labels
    xaxis_cfg = dict(
        tickmode='array',
        tickvals=tick_vals,
        ticktext=tick_text,
        tickangle=-45,
        title='Brick-Index (Datum)',
    )
    fig.update_xaxes(xaxis_cfg)
    fig.update_yaxes(title_text='Preis', row=1, col=1)
    fig.update_yaxes(title_text='Vol',   row=2, col=1)
    fig.update_yaxes(title_text='ATR',   row=3, col=1)

    # Speichern
    os.makedirs(CHARTS_DIR, exist_ok=True)
    safe_name  = symbol.replace('/', '').replace(':', '')
    ts_stamp   = datetime.now().strftime('%Y%m%d_%H%M%S')
    chart_path = os.path.join(CHARTS_DIR, f'chart_{safe_name}_{timeframe}_{ts_stamp}.html')
    fig.write_html(chart_path)
    return chart_path


def _send_via_telegram(chart_paths: list, bot_token: str, chat_id: str):
    import requests
    for path in chart_paths:
        filename = os.path.basename(path)
        caption  = f'ZeroBot EAR-Chart: {filename.replace("chart_", "").replace(".html", "")}'
        try:
            with open(path, 'rb') as f:
                requests.post(
                    f'https://api.telegram.org/bot{bot_token}/sendDocument',
                    data={'chat_id': chat_id, 'caption': caption},
                    files={'document': (filename, f, 'text/html')},
                    timeout=60,
                )
        except Exception as e:
            print(f'  Telegram-Fehler: {e}')


def run_interactive_chart():
    """Interaktiver Chart-Generator (Modus 4)."""
    print('\n========== INTERAKTIVE CHARTS ===========\n')

    configs = _load_all_configs()
    if not configs:
        print(f'{RED}Keine Config-Dateien gefunden in {CONFIGS_DIR}{NC}')
        print(f'{YELLOW}Bitte zuerst run_pipeline.sh ausfuehren.{NC}')
        return

    print(f'{BOLD}{"=" * 70}{NC}')
    print('Verfuegbare Konfigurationen:')
    print(f'{BOLD}{"=" * 70}{NC}')
    for idx, cfg in enumerate(configs, 1):
        meta    = cfg.get('_meta', {})
        pnl     = meta.get('pnl_pct')
        pnl_str = (f'  [+{pnl:.1f}%]' if pnl and pnl > 0
                   else f'  [{pnl:.1f}%]' if pnl is not None else '')
        clean   = cfg['_filename'].replace('config_', '').replace('.json', '')
        print(f'{idx:>3}) {clean:<34}{CYAN}{pnl_str}{NC}')
    print(f'{BOLD}{"=" * 70}{NC}')

    print('\nWaehle Konfiguration(en) zum Anzeigen:')
    print("  Einzeln: z.B. '1' oder '5'")
    print("  Mehrfach: z.B. '1,3,5' oder '1 3 5'")
    raw = input('\nAuswahl: ').strip().lower()
    if raw in ('alle', 'all'):
        selected = configs
    else:
        indices = []
        for part in raw.replace(',', ' ').split():
            try:
                indices.append(int(part) - 1)
            except ValueError:
                pass
        selected = [configs[i] for i in indices if 0 <= i < len(configs)]

    if not selected:
        print(f'{RED}Keine gueltigen Strategien ausgewaehlt.{NC}')
        return

    print(f'\n{"=" * 60}')
    print('Chart-Optionen:')
    print(f'{"=" * 60}')

    raw = input('Startdatum (JJJJ-MM-TT) [leer=2024-01-01]: ').strip()
    start_date = raw if raw else '2024-01-01'

    raw = input('Enddatum (JJJJ-MM-TT) [leer=heute]: ').strip()
    end_date = raw if raw else datetime.now(timezone.utc).strftime('%Y-%m-%d')

    raw = input('Letzten N Tage anzeigen [leer=alle]: ').strip()
    if raw:
        try:
            from datetime import timedelta
            n_days   = int(raw)
            end_dt   = datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(days=n_days)
            start_date = start_dt.strftime('%Y-%m-%d')
            end_date   = end_dt.strftime('%Y-%m-%d')
        except ValueError:
            pass

    raw = input('Startkapital in USDT [Standard: 100]: ').strip()
    try:
        start_capital = float(raw) if raw else 100.0
    except ValueError:
        start_capital = 100.0

    # OOS-Modus: Warmup-Start fuer korrekten Brick-State (optional)
    warmup_start    = None
    trade_start_date = None
    oos_file = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')
    if os.path.exists(oos_file):
        try:
            with open(oos_file) as f:
                oos_data = json.load(f)
            suggested_ws  = oos_data.get('warmup_start', '')
            suggested_oos = oos_data.get('oos_start', '')
            if suggested_ws and suggested_oos:
                print(f'\n  Letzter OOS-Test erkannt:')
                print(f'  Warmup ab: {suggested_ws}  |  OOS ab: {suggested_oos}')
                raw = input('  OOS-Modus aktivieren? (j/n) [Standard: n]: ').strip().lower()
                if raw in ('j', 'y', 'ja', 'yes'):
                    warmup_start     = suggested_ws
                    trade_start_date = suggested_oos
                    print(f'  {GREEN}OOS-Modus aktiv: Warmup={warmup_start}, Trades ab={trade_start_date}{NC}')
        except Exception:
            pass
    if warmup_start is None:
        raw = input('\nWarmup-Startdatum fuer OOS-Modus (JJJJ-MM-TT) [leer=aus]: ').strip()
        if raw:
            warmup_start     = raw
            trade_start_date = start_date
            print(f'  {GREEN}OOS-Modus aktiv: Warmup={warmup_start}, Trades ab={trade_start_date}{NC}')

    # Telegram
    bot_token, chat_id = '', ''
    send_tg = False
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            secrets = json.load(f)
        tg        = secrets.get('telegram', {})
        bot_token = tg.get('bot_token', '')
        chat_id   = tg.get('chat_id', '')
    except Exception:
        pass
    if bot_token and chat_id:
        raw = input('Telegram versenden? (j/n) [Standard: n]: ').strip().lower()
        send_tg = raw in ('j', 'y', 'ja', 'yes')

    generated = []
    for cfg in selected:
        market   = cfg.get('market', {})
        symbol   = market.get('symbol', '?')
        tf       = market.get('timeframe', '?')
        strategy = cfg.get('strategy', {})
        risk     = cfg.get('risk', {})

        print(f'INFO: Verarbeite {cfg["_filename"]}...')
        path = _generate_chart(symbol, tf, start_date, end_date,
                               start_capital, strategy, risk,
                               trade_start_date=trade_start_date,
                               warmup_start=warmup_start)
        if path:
            generated.append(path)
            print(f'INFO: Erstelle Chart...')
            print(f'INFO: {GREEN}✅ Chart gespeichert: {path}{NC}')
            if send_tg:
                print(f'INFO: Sende Chart via Telegram...')
                _send_via_telegram([path], bot_token, chat_id)

    if not generated:
        print(f'\n{RED}Keine Charts generiert.{NC}')
        return

    print(f'\nINFO:')
    print(f'INFO: {GREEN}✅ Alle Charts generiert!{NC}')
    print(f'{GREEN}✅ Charts wurden generiert!{NC}')


if __name__ == '__main__':
    run_interactive_chart()
