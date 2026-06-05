# src/zerobot/analysis/interactive_chart.py
"""
ZeroBot Interaktive Charts (Modus 4)

Generiert Plotly-HTML mit:
  - Candlestick-Chart mit Renko-Signal-Markern
  - Entry/Exit Trade-Marker (Long/Short, TP/SL)
  - Equity-Curve Subplot (rechte Y-Achse)
  - Volumen-Panel
  - ATR-Panel
"""

import os
import sys
import json
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


def _generate_chart(symbol: str, timeframe: str,
                    start_date: str, end_date: str,
                    start_capital: float,
                    strategy_params: dict, risk_params: dict) -> str:
    """Generiert HTML-Chart fuer ein Symbol. Gibt Pfad zur HTML-Datei zurueck."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        import ta
    except ImportError:
        print(f'{RED}Fehler: plotly / ta-lib nicht installiert.{NC}')
        return ''

    from zerobot.analysis.backtester import load_data, run_backtest
    from zerobot.strategy.renko_engine import RenkoEngine

    print(f'INFO: Lade OHLCV-Daten fuer {symbol} {timeframe}...')
    df = load_data(symbol, timeframe, start_date, end_date)
    if df is None or df.empty:
        print(f'INFO: {RED}Keine Daten verfuegbar fuer {symbol} ({timeframe}).{NC}')
        return ''

    # ATR berechnen
    atr_indicator = ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'], window=14)
    df['atr'] = atr_indicator.average_true_range()
    df.dropna(subset=['atr'], inplace=True)

    # Renko-Signale hinzufuegen
    engine    = RenkoEngine(settings=strategy_params)
    df_renko  = engine.process_dataframe(df.copy())

    # Renko-Signalpunkte
    long_sig_mask  = df_renko['renko_signal'] == 1
    short_sig_mask = df_renko['renko_signal'] == -1

    print(f'INFO: Fuehre Backtest durch...')
    res    = run_backtest(df.copy(), strategy_params, risk_params,
                          start_capital=start_capital, return_trades=True)
    trades = res.get('trades', [])

    # Equity-Kurve
    cap_times = [str(df.index[0])]
    cap_vals  = [start_capital]
    for t in trades:
        cap_times.append(t.get('exit_time', ''))
        cap_vals.append(t.get('capital_after', start_capital))

    pnl_pct  = res.get('total_pnl_pct', 0.0)
    win_rate = res.get('win_rate', 0.0)
    max_dd   = res.get('max_drawdown_pct', 0.0)
    n_trades = res.get('trades_count', 0)

    # Trade-Listen
    long_entries  = [t for t in trades if t.get('side') == 'long']
    short_entries = [t for t in trades if t.get('side') == 'short']
    tp_exits      = [t for t in trades if t.get('win')]
    sl_exits      = [t for t in trades if not t.get('win')]

    # Figur: 3 Panels
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        specs=[
            [{'secondary_y': True}],
            [{'secondary_y': False}],
            [{'secondary_y': False}],
        ],
        vertical_spacing=0.020,
        row_heights=[0.60, 0.15, 0.25],
        subplot_titles=['', 'Volumen', 'ATR  (Stop-Loss-Basis)'],
    )

    # --- Panel 1: Candlestick ---
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df['open'], high=df['high'],
        low=df['low'],   close=df['close'],
        name='OHLC',
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        showlegend=True,
    ), row=1, col=1, secondary_y=False)

    # Renko LONG-Signale (kleine grüne Diamanten)
    if long_sig_mask.any():
        fig.add_trace(go.Scatter(
            x=df_renko.index[long_sig_mask],
            y=df_renko.loc[long_sig_mask, 'low'] * 0.998,
            mode='markers',
            marker=dict(symbol='diamond', size=8, color='#26a69a',
                        line=dict(color='#ffffff', width=0.5)),
            name='Renko LONG',
            hovertemplate='Renko LONG<br>%{x}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Renko SHORT-Signale (kleine rote Diamanten)
    if short_sig_mask.any():
        fig.add_trace(go.Scatter(
            x=df_renko.index[short_sig_mask],
            y=df_renko.loc[short_sig_mask, 'high'] * 1.002,
            mode='markers',
            marker=dict(symbol='diamond', size=8, color='#ef5350',
                        line=dict(color='#ffffff', width=0.5)),
            name='Renko SHORT',
            hovertemplate='Renko SHORT<br>%{x}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Entry Long (grüne Dreiecke)
    if long_entries:
        fig.add_trace(go.Scatter(
            x=[t['entry_time'] for t in long_entries],
            y=[t['entry_price'] for t in long_entries],
            mode='markers',
            marker=dict(symbol='triangle-up', size=16, color='#26a69a',
                        line=dict(color='#ffffff', width=1)),
            name='Entry Long ▲',
            hovertemplate='Entry Long<br>%{x}<br>Preis: %{y:.4f}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Entry Short (orange Dreiecke)
    if short_entries:
        fig.add_trace(go.Scatter(
            x=[t['entry_time'] for t in short_entries],
            y=[t['entry_price'] for t in short_entries],
            mode='markers',
            marker=dict(symbol='triangle-down', size=16, color='#ffa726',
                        line=dict(color='#ffffff', width=1)),
            name='Entry Short ▼',
            hovertemplate='Entry Short<br>%{x}<br>Preis: %{y:.4f}<extra></extra>',
        ), row=1, col=1, secondary_y=False)

    # Exit TP (cyan Kreise)
    if tp_exits:
        fig.add_trace(go.Scatter(
            x=[t['exit_time'] for t in tp_exits],
            y=[t['exit_price'] for t in tp_exits],
            mode='markers',
            marker=dict(symbol='circle', size=13, color='#00bcd4',
                        line=dict(color='#ffffff', width=1)),
            name='Exit TP ✓',
            hovertemplate='Exit TP<br>%{x}<br>Preis: %{y:.4f}<br>PnL: %{customdata:.4f} USDT<extra></extra>',
            customdata=[t.get('pnl_usd', 0) for t in tp_exits],
        ), row=1, col=1, secondary_y=False)

    # Exit SL (rote ×)
    if sl_exits:
        fig.add_trace(go.Scatter(
            x=[t['exit_time'] for t in sl_exits],
            y=[t['exit_price'] for t in sl_exits],
            mode='markers',
            marker=dict(symbol='x', size=14, color='#ef5350',
                        line=dict(color='#ef5350', width=3)),
            name='Exit SL ✗',
            hovertemplate='Exit SL<br>%{x}<br>Preis: %{y:.4f}<br>PnL: %{customdata:.4f} USDT<extra></extra>',
            customdata=[t.get('pnl_usd', 0) for t in sl_exits],
        ), row=1, col=1, secondary_y=False)

    # Equity-Kurve (rechte Y-Achse)
    fig.add_trace(go.Scatter(
        x=cap_times,
        y=cap_vals,
        mode='lines',
        line=dict(color='#5c9bd6', width=1.5),
        name='Equity',
        hovertemplate='Equity: %{y:.2f} USDT<extra></extra>',
    ), row=1, col=1, secondary_y=True)

    # --- Panel 2: Volumen ---
    if 'volume' in df.columns:
        vol_colors = ['#26a69a' if c >= o else '#ef5350'
                      for c, o in zip(df['close'], df['open'])]
        fig.add_trace(go.Bar(
            x=df.index, y=df['volume'],
            marker_color=vol_colors,
            name='Volumen', showlegend=False, opacity=0.65,
        ), row=2, col=1)

    # --- Panel 3: ATR ---
    atr_ser = df['atr']
    fig.add_trace(go.Scatter(
        x=df.index, y=atr_ser,
        mode='lines', line=dict(color='#42a5f5', width=1.3),
        fill='tozeroy', fillcolor='rgba(66,165,245,0.08)',
        name='ATR', showlegend=False,
        hovertemplate='ATR: %{y:.4f}<extra></extra>',
    ), row=3, col=1)

    # SL-Abstand auf ATR-Panel markieren (Entry-Punkte als Kreis)
    if trades:
        atr_mult = risk_params.get('atr_multiplier_sl', 2.0)
        for t in trades:
            try:
                ts_key = t['entry_time']
                atr_at_entry = float(atr_ser.asof(
                    atr_ser.index[atr_ser.index.get_indexer([ts_key], method='nearest')[0]]
                )) if hasattr(atr_ser.index, 'get_indexer') else float(atr_ser.mean())
            except Exception:
                atr_at_entry = float(atr_ser.mean())
        # Batch: alle Entry-ATR-Punkte
        entry_times = [t['entry_time'] for t in trades]
        entry_atrs  = []
        for et in entry_times:
            try:
                idx = atr_ser.index.get_indexer([et], method='nearest')[0]
                entry_atrs.append(float(atr_ser.iloc[idx]))
            except Exception:
                entry_atrs.append(float(atr_ser.mean()))
        fig.add_trace(go.Scatter(
            x=entry_times, y=entry_atrs, mode='markers',
            marker=dict(symbol='circle-open', size=9, color='#ffa726',
                        line=dict(width=2)),
            showlegend=False,
            hovertemplate='ATR bei Entry: %{y:.4f}<extra></extra>',
        ), row=3, col=1)

    # ATR-MA als Referenzlinie
    atr_ma = atr_ser.rolling(20, min_periods=1).mean()
    fig.add_trace(go.Scatter(
        x=df.index, y=atr_ma,
        mode='lines', line=dict(color='rgba(255,167,38,0.5)', width=1, dash='dot'),
        name='ATR MA(20)', showlegend=False,
        hovertemplate='ATR-MA: %{y:.4f}<extra></extra>',
    ), row=3, col=1)

    # Layout
    sign       = '+' if pnl_pct >= 0 else ''
    title_text = (
        f'{symbol} {timeframe} — ZeroBot Renko | '
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
        margin=dict(l=60, r=70, t=80, b=40),
        yaxis2=dict(title='Equity (USDT)', showgrid=False,
                    tickfont=dict(color='#5c9bd6'),
                    title_font=dict(color='#5c9bd6')),
    )
    fig.update_yaxes(title_text='Preis', row=1, col=1)
    fig.update_yaxes(title_text='Vol',   row=2, col=1)
    fig.update_yaxes(title_text='ATR',   row=3, col=1)

    os.makedirs(CHARTS_DIR, exist_ok=True)
    safe_name  = symbol.replace('/', '').replace(':', '')
    ts         = datetime.now().strftime('%Y%m%d_%H%M%S')
    chart_path = os.path.join(CHARTS_DIR, f'chart_{safe_name}_{timeframe}_{ts}.html')
    fig.write_html(chart_path)
    return chart_path


def _send_via_telegram(chart_paths: list, bot_token: str, chat_id: str):
    import requests
    for path in chart_paths:
        filename = os.path.basename(path)
        caption  = f'ZeroBot Chart: {filename.replace("chart_", "").replace(".html", "")}'
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
        pnl_str = f'  [+{pnl:.1f}%]' if pnl and pnl > 0 else (
                  f'  [{pnl:.1f}%]' if pnl is not None else '')
        clean   = cfg['_filename'].replace('config_', '').replace('.json', '')
        mkt     = cfg.get('market', {})
        sym_str = f"{mkt.get('symbol', '?')} {mkt.get('timeframe', '?')}"
        print(f'{idx:>3}) {clean:<30}{CYAN}{pnl_str}{NC}')
    print(f'{BOLD}{"=" * 70}{NC}')

    print('\nWaehle Konfiguration(en) zum Anzeigen:')
    print("  Einzeln: z.B. '1' oder '5'")
    print("  Mehrfach: z.B. '1,3,5' oder '1 3 5'")
    raw = input('\nAuswahl: ').strip().lower()
    if raw in ('alle', 'all'):
        selected = configs
    else:
        indices  = []
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

    # Charts generieren
    generated = []
    for cfg in selected:
        market   = cfg.get('market', {})
        symbol   = market.get('symbol', '?')
        tf       = market.get('timeframe', '?')
        strategy = cfg.get('strategy', {})
        risk     = cfg.get('risk', {})

        print(f'INFO: Verarbeite {cfg["_filename"]}...')
        path = _generate_chart(symbol, tf, start_date, end_date,
                               start_capital, strategy, risk)
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
