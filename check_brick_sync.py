# check_brick_sync.py
"""
Vergleicht fuer jedes aktive Symbol die live persistierte EAR-Brick-Kette
(artifacts/db/ear_brick_state_*.json) gegen eine frisch, nie neu verankerte
Referenzkette aus den letzten ROLLING_WINDOW_DAYS Tagen Kursdaten (exakt wie
_bootstrap_brick_chain/backtester._build_bricks, nur mit rollierendem statt
festem Warmup-Start -- Renko/EAR-Ketten sind stark pfadabhaengig und
akkumulieren Drift ueber Wochen, siehe research_zerobot_live_vs_backtest_2026_09).

Bei Richtungs-Abweichung (das kritische Signal -- Live und Referenz zeigen
entgegengesetzten Trend):
  1. Telegram-Alarm "Abweichung erkannt".
  2. Korrektur NUR wenn aktuell keine Position fuer das Symbol offen ist
     (sonst wuerde man einem laufenden Trade die Gegenbrick-Definition
     unter den Fuessen wegziehen -- eigenes Risiko). Bei offener Position:
     Telegram "Korrektur verschoben", kein erneuter Alarm bei Folge-Laeufen
     fuer dieselbe Abweichung (Dedup ueber pending-Marker-Datei).
  3. Nach erfolgter Korrektur: Telegram-Bestaetigung.

Gedacht fuer denselben 15-Min-Cron-Rhythmus wie master_runner.py, eigener
Lock (flock) noetig.
"""
import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.strategy.ear_engine import EAREngine
from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message, send_photo
from zerobot.utils.strategy_list import add_orphaned_open_positions as _add_orphaned_open_positions

DB_PATH        = os.path.join(PROJECT_ROOT, 'artifacts', 'db')
OHLCV_CACHE    = os.path.join(DB_PATH, 'brick_sync_cache')
CONFIGS_DIR    = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
RESULTS_FILE   = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'optimization_results.json')
PENDING_PREFIX = os.path.join(DB_PATH, 'brick_sync_pending_')

ROLLING_WINDOW_DAYS = 100   # Referenzketten-Warmup -- genug fuer Konvergenz,
                            # klein genug fuer schnelles, wiederholtes Neuladen


def setup_logging():
    log_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger('brick_sync')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(os.path.join(log_dir, 'brick_sync.log'),
                                 maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
        logger.addHandler(ch)
        logger.propagate = False
    return logger


def load_rolling_ohlcv(exchange, symbol, timeframe, logger):
    """Rollierender lokaler Cache (eigenstaendig von backtester.load_data,
    damit ein 'heute' als Endzeitpunkt nicht bei jedem Lauf einen vollen
    Jahre-Refetch ausloest -- nur die neuesten Kerzen kommen bei jedem Lauf dazu.

    Nutzt bewusst fetch_historical_ohlcv (nicht fetch_ohlcv_since): Live
    verifiziert (2026-09-11) liefert Bitget fuer eine grosse Zeitspanne (hier:
    100 Tage) auf der allerersten Page still nur ~199 statt 200 Kerzen (deckt
    nur ~8 Tage ab, KEIN Fehler/leere Antwort) -- fetch_ohlcv_since wertet
    'batch kuerzer als angefordert' faelschlich als 'Ende der Historie' und
    bricht sofort ab (das ist fuer seinen eigentlichen Zweck, kleine
    Live-Cron-Nachlade-Haeppchen, unproblematisch, aber genau falsch fuer
    einen grossen Warmup-Fetch). fetch_historical_ohlcv hat diese Annahme
    nicht (paged bis end_ts erreicht ist, unabhaengig von der Page-Groesse)
    und ist bereits an anderer Stelle im Projekt fuer genau diesen Fall
    (grosser Bootstrap-Fetch) verifiziert -- siehe _bootstrap_brick_chain."""
    os.makedirs(OHLCV_CACHE, exist_ok=True)
    safe = symbol.replace('/', '-').replace(':', '-')
    cache_file = os.path.join(OHLCV_CACHE, f'{safe}_{timeframe}.csv')

    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=ROLLING_WINDOW_DAYS)
    df = pd.DataFrame()
    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col='ts', parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
        except Exception as e:
            logger.warning(f"Cache-Lesefehler {cache_file}: {e}")
            df = pd.DataFrame()

    start_dt = cutoff if df.empty else (df.index.max() + pd.Timedelta(milliseconds=1))
    end_dt   = pd.Timestamp.now(tz='UTC') + pd.Timedelta(days=1)  # inkl. heute

    new_data = exchange.fetch_historical_ohlcv(
        symbol, timeframe, start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'))
    if not new_data.empty:
        new_data = new_data[new_data.index >= start_dt]
        df = pd.concat([df, new_data]) if not df.empty else new_data
        df = df[~df.index.duplicated(keep='last')].sort_index()

    df = df[df.index >= cutoff]
    if not df.empty:
        df.index.name = 'ts'  # exchange.py's fetch_* nennen den Index 'timestamp' --
                              # hier fest auf 'ts' normalisiert, damit Schreiben (hier)
                              # und Lesen (index_col='ts' oben) garantiert zusammenpassen,
                              # unabhaengig davon wie die jeweilige Fetch-Funktion benennt.
        df.to_csv(cache_file)
    return df


def get_active_strategies(settings, logger):
    """Repliziert exakt master_runner.main()'s Bestimmung der aktiven Symbole --
    liest bei jedem Lauf frisch aus settings.json, damit woechentliche
    Optimizer-Aenderungen an active_strategies automatisch mitgezogen werden.
    Gibt Liste von (symbol, timeframe) zurueck, inkl. verwaister-aber-offener
    Positionen (siehe _add_orphaned_open_positions)."""
    live_settings = settings.get('live_trading_settings', {})
    use_autopilot = live_settings.get('use_auto_optimizer_results', False)

    if use_autopilot:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE) as f:
                strategy_config = json.load(f)
            strategy_list = strategy_config.get('optimal_portfolio', [])
        else:
            logger.warning("Autopilot-Modus, aber keine Optimierungs-Ergebnisse gefunden.")
            strategy_list = []
    else:
        strategy_list = live_settings.get('active_strategies', [])

    strategy_list = _add_orphaned_open_positions(list(strategy_list), PROJECT_ROOT)

    pairs = []
    for entry in strategy_list:
        if isinstance(entry, dict):
            if not entry.get('active', True):
                continue
            symbol, timeframe = entry.get('symbol'), entry.get('timeframe')
        elif isinstance(entry, str):
            cfg_path = os.path.join(CONFIGS_DIR, entry)
            if not os.path.exists(cfg_path):
                continue
            with open(cfg_path) as f:
                c_data = json.load(f)
            symbol, timeframe = c_data['market']['symbol'], c_data['market']['timeframe']
        else:
            continue
        if symbol and timeframe:
            pairs.append((symbol, timeframe))
    return pairs


TF_MINUTES = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120, '4h': 240, '6h': 360, '1d': 1440}


def build_reference_chain(exchange, symbol, timeframe, strat_params, up_to_ts, logger):
    """Baut die Referenzkette -- inkl. Plausibilitaetspruefung: bei einem
    stillen Fetch-Abbruch (z.B. Rate-Limit waehrend der Pagination in
    load_rolling_ohlcv/fetch_ohlcv_since) waere die Kerzenzahl verdaechtig
    niedrig fuer das angefragte Fenster -- lieber None (= 'unzuverlaessig,
    ueberspringen') als eine kaputte Referenz, gegen die faelschlich
    alarmiert oder sogar korrigiert wuerde."""
    df = load_rolling_ohlcv(exchange, symbol, timeframe, logger)
    if df.empty:
        return None
    df = df[df.index <= up_to_ts]

    tf_min = TF_MINUTES.get(timeframe)
    if tf_min:
        expected = (ROLLING_WINDOW_DAYS * 24 * 60) / tf_min
        if len(df) < expected * 0.7:
            logger.warning(f"{symbol} ({timeframe}): nur {len(df)}/{expected:.0f} erwartete Kerzen geladen "
                          f"-- vermutlich unvollstaendiger Fetch (Rate-Limit?), Referenz verworfen.")
            return None

    if len(df) < 20:
        return None
    engine = EAREngine(settings=strat_params)
    bricks = engine._build_bricks(df)
    if not bricks:
        return None
    return bricks


def _generate_sync_chart(symbol, tf, top_label, top_bricks, bottom_label, bottom_bricks,
                        title_suffix, logger, n_bricks=20):
    """Zwei uebereinanderliegende Brick-Streifen (gleiche Preisachse, gleicher
    Stil wie trade_manager._generate_brick_png: dunkler Hintergrund, gruen=up,
    rot=down) -- macht eine Ketten-Abweichung oder eine frische Korrektur auf
    einen Blick sichtbar, statt nur als Prozentzahl im Text."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        logger.warning("matplotlib nicht verfuegbar -- kein Vergleichs-Chart.")
        return None

    rows = [(top_label, top_bricks[-n_bricks:] if top_bricks else []),
            (bottom_label, bottom_bricks[-n_bricks:] if bottom_bricks else [])]

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=False)
    fig.patch.set_facecolor('#0d1117')

    all_prices = [c for _, bricks in rows for _, c in bricks]
    if not all_prices:
        plt.close(fig)
        return None
    y_min, y_max = min(all_prices), max(all_prices)
    margin = (y_max - y_min) * 0.15 or y_min * 0.01

    for ax, (label, bricks) in zip(axes, rows):
        ax.set_facecolor('#0d1117')
        n = len(bricks)
        for i, (direction, close) in enumerate(bricks):
            prev_c = bricks[i - 1][1] if i > 0 else close
            is_up  = direction == 'up'
            color  = '#26a69a' if is_up else '#ef5350'
            bottom = min(prev_c, close)
            height = abs(close - prev_c) or abs(close) * 1e-5
            rect = mpatches.FancyBboxPatch((i - 0.4, bottom), 0.8, height,
                                          boxstyle="square,pad=0", linewidth=0.5,
                                          edgecolor='#1e2a3a', facecolor=color, zorder=2)
            ax.add_patch(rect)
        if bricks:
            last_dir, last_close = bricks[-1]
            ax.axhline(last_close, color='#ffd700', linewidth=1.0, linestyle='--', zorder=3)
            ax.text(n - 0.5, last_close, f"  {last_dir.upper()}\n  {last_close:.6g}",
                   color='#ffd700', fontsize=8, va='center', ha='left')
        ax.set_xlim(-1, max(n, 1))
        ax.set_ylim(y_min - margin, y_max + margin)
        ax.set_title(label, color='#e0e0e0', fontsize=10, loc='left', pad=6)
        ax.tick_params(colors='#888888', labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor('#2a3a4a')
        ax.set_xticks([])
        ax.yaxis.tick_right()
        ax.grid(axis='y', color='#1e2a3a', linewidth=0.5, zorder=1)

    fig.suptitle(f"{symbol}  {tf}  |  {title_suffix}", color='#e0e0e0', fontsize=11)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    tmp_dir = os.path.join(PROJECT_ROOT, 'artifacts', 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    sym_safe = symbol.replace('/', '-').replace(':', '-')
    path     = os.path.join(tmp_dir, f'brick_sync_{sym_safe}_{tf}_{ts}.png')
    fig.savefig(path, dpi=130, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def _send_sync_chart(telegram_config, path, caption, logger):
    if not path or not telegram_config.get('bot_token') or not telegram_config.get('chat_id'):
        return
    try:
        send_photo(telegram_config['bot_token'], telegram_config['chat_id'], path, caption=caption)
    except Exception as e:
        logger.warning(f"Vergleichs-Chart konnte nicht gesendet werden: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)


def run(dry_run=False):
    """Ein Durchlauf des Brick-Sync-Checks. dry_run=True: nur pruefen/loggen,
    kein Telegram, keine Datei-Korrektur. Importierbar (z.B. aus
    master_runner.py, statt als eigener Cronjob) oder per CLI (siehe main())."""
    logger = setup_logging()
    logger.info(f"--- Brick-Sync-Check gestartet{' (DRY-RUN)' if dry_run else ''} ---")

    with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
        secrets = json.load(f)
    with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
        settings = json.load(f)
    overrides       = settings.get('strategy_overrides', {})
    telegram_config = secrets.get('telegram', {})
    account         = secrets['zerobot'][0]

    def tg(msg):
        logger.info(msg.replace('\n', ' | '))
        if dry_run:
            try:
                print(f"[DRY-RUN TELEGRAM] {msg}")
            except UnicodeEncodeError:
                print(f"[DRY-RUN TELEGRAM] {msg.encode('ascii', 'replace').decode()}")
            return
        if telegram_config.get('bot_token') and telegram_config.get('chat_id'):
            send_message(telegram_config['bot_token'], telegram_config['chat_id'], msg)

    exchange = Exchange(account)
    if not exchange.markets:
        logger.critical("Exchange konnte nicht initialisiert werden.")
        return

    active_pairs = get_active_strategies(settings, logger)
    if not active_pairs:
        logger.warning("Keine aktiven Strategien in settings.json gefunden.")
        return
    logger.info(f"Aktive Strategien ({len(active_pairs)}): " +
               ", ".join(f"{s} ({tf})" for s, tf in active_pairs))

    for symbol, tf in active_pairs:
        safe = symbol.replace('/', '').replace(':', '')
        path = os.path.join(DB_PATH, f"ear_brick_state_{symbol.replace('/', '-').replace(':', '-')}_{tf}.json")

        if not os.path.exists(path):
            logger.info(f"{symbol} ({tf}): noch kein Brick-State persistiert (neu aktiviert?), ueberspringe.")
            continue
        try:
            with open(path) as f:
                live = json.load(f)
        except Exception as e:
            logger.warning(f"{symbol} ({tf}): State nicht lesbar: {e}")
            continue

        if not all(k in live for k in ('lc', 'direction', 'last_processed_ts')):
            continue

        cfg_path = os.path.join(CONFIGS_DIR, f'config_{safe}_{tf}.json')
        if not os.path.exists(cfg_path):
            logger.info(f"{symbol} ({tf}): keine Config gefunden, ueberspringe.")
            continue
        with open(cfg_path) as f:
            cfg = json.load(f)
        strat = dict(cfg.get('strategy', {}))
        strat.update(overrides)

        pending_file = f"{PENDING_PREFIX}{safe}_{tf}.json"

        try:
            up_to_ts = pd.Timestamp(live['last_processed_ts'])
            ref_bricks = build_reference_chain(exchange, symbol, tf, strat, up_to_ts, logger)
        except Exception as e:
            logger.error(f"{symbol} ({tf}): Referenzketten-Aufbau fehlgeschlagen: {e}", exc_info=True)
            continue

        if not ref_bricks:
            logger.info(f"{symbol} ({tf}): keine Referenz-Bricks verfuegbar, ueberspringe.")
            continue

        ref_lc  = ref_bricks[-1]['close']
        ref_dir = ref_bricks[-1]['direction']

        # Zweite, unabhaengige Plausibilitaetspruefung: Referenz-lc gegen echten
        # Ticker-Preis. Ein Datenfehler (z.B. Rate-Limit) kann eine Kette mit
        # plausibler Kerzenzahl, aber falschem/veraltetem Preisniveau erzeugen --
        # das darf nie als "Abweichung" fehlalarmiert werden.
        ticker = exchange.fetch_ticker(symbol)
        if ticker and ticker.get('last'):
            ticker_dev = abs(ref_lc - ticker['last']) / ticker['last'] * 100
            if ticker_dev > 15.0:
                logger.warning(f"{symbol} ({tf}): Referenz-lc {ref_lc:.6g} liegt {ticker_dev:.1f}% "
                              f"vom aktuellen Ticker ({ticker['last']:.6g}) entfernt -- unplausibel, "
                              f"verwerfe Referenz statt zu alarmieren.")
                continue

        dev_pct = abs(ref_lc - live['lc']) / live['lc'] * 100 if live['lc'] else 0.0
        dir_match = (ref_dir == live['direction'])

        logger.info(f"{symbol} ({tf}): live={live['direction']}@{live['lc']:.6g} "
                   f"ref={ref_dir}@{ref_lc:.6g} dev={dev_pct:.2f}% match={dir_match}")

        if dir_match:
            if os.path.exists(pending_file):
                os.remove(pending_file) if not dry_run else None
                logger.info(f"{symbol} ({tf}): Abweichung hat sich von selbst aufgeloest, Marker entfernt.")
            continue

        already_alerted = os.path.exists(pending_file)
        live_recent_bricks = [tuple(x) for x in live.get('recent_bricks', [])]
        ref_recent_bricks  = [(b['direction'], b['close']) for b in ref_bricks]

        if not already_alerted:
            tg(
                f"⚠️ ZEROBOT Brick-Kette weicht ab: {symbol} ({tf})\n"
                f"- Live-Kette: {live['direction'].upper()} @ {live['lc']:.6g}\n"
                f"- Referenz (frisch, {ROLLING_WINDOW_DAYS}d Kursdaten): {ref_dir.upper()} @ {ref_lc:.6g}\n"
                f"- Preis-Abweichung: {dev_pct:.2f}%\n"
                f"Pruefe auf offene Position..."
            )
            chart_path = _generate_sync_chart(
                symbol, tf,
                f"Live-Kette (persistiert) -- {live['direction'].upper()}", live_recent_bricks,
                f"Referenz-Kette (frisch, {ROLLING_WINDOW_DAYS}d) -- {ref_dir.upper()}", ref_recent_bricks,
                f"Abweichung erkannt ({dev_pct:.2f}%)", logger)
            if not dry_run:
                _send_sync_chart(telegram_config, chart_path,
                                f"{symbol} ({tf}): Brick-Abweichung {dev_pct:.2f}%", logger)
            elif chart_path:
                print(f"[DRY-RUN] Vergleichs-Chart erzeugt: {chart_path}")
            if not dry_run:
                with open(pending_file, 'w') as f:
                    json.dump({'detected_at': datetime.now(timezone.utc).isoformat(),
                               'live_lc': live['lc'], 'ref_lc': ref_lc}, f)

        open_pos = exchange.fetch_open_positions(symbol)
        if open_pos:
            if not already_alerted:
                tg(f"⏳ ZEROBOT: Korrektur fuer {symbol} ({tf}) verschoben — "
                   f"Position ist noch offen. Wird nach Schliessung automatisch nachgeholt.")
            else:
                logger.info(f"{symbol} ({tf}): Abweichung weiter bekannt, Position noch offen -- warte.")
            continue

        new_state = {
            'lc': ref_lc,
            'direction': ref_dir,
            'last_processed_ts': live['last_processed_ts'],
            'recent_bricks': [[b['direction'], b['close']] for b in ref_bricks[-20:]],
        }

        if dry_run:
            print(f"[DRY-RUN] wuerde {path} korrigieren auf: {new_state['direction']} @ {new_state['lc']:.6g}")
            written = new_state
        else:
            with open(path, 'w') as f:
                json.dump(new_state, f)
            # Echte Verifikation statt Wiederverwendung der im Speicher gehaltenen
            # Werte: Datei zurueck von der Platte lesen, damit die Bestaetigung
            # einen tatsaechlich erfolgten (und korrekt lesbaren) Schreibvorgang
            # belegt, nicht nur behauptet.
            try:
                with open(path) as f:
                    written = json.load(f)
            except Exception as e:
                tg(f"🛑 ZEROBOT: Korrektur fuer {symbol} ({tf}) geschrieben, aber "
                   f"Rueck-Lesen zur Verifikation fehlgeschlagen: {e}. Bitte manuell pruefen!")
                continue
            if written.get('direction') != ref_dir or abs(written.get('lc', 0) - ref_lc) > 1e-12:
                tg(f"🛑 ZEROBOT: Korrektur fuer {symbol} ({tf}) verifiziert FEHLGESCHLAGEN "
                   f"-- Datei zeigt {written.get('direction')}@{written.get('lc')}, "
                   f"erwartet war {ref_dir}@{ref_lc:.6g}. Bitte manuell pruefen!")
                continue
            if os.path.exists(pending_file):
                os.remove(pending_file)

        tg(f"✅ ZEROBOT: Brick-Kette fuer {symbol} ({tf}) korrigiert "
           f"(neu: {ref_dir.upper()} @ {ref_lc:.6g}, Abweichung war {dev_pct:.2f}%)"
           f"{' -- von der Platte zurueckgelesen bestaetigt' if not dry_run else ''}.")
        written_recent = [tuple(x) for x in written.get('recent_bricks', [])]
        fix_chart_path = _generate_sync_chart(
            symbol, tf,
            f"Vorher (fehlerhaft) -- {live['direction'].upper()}", live_recent_bricks,
            f"Jetzt (korrigiert, von Platte gelesen) -- {written.get('direction', ref_dir).upper()}", written_recent,
            f"Korrigiert (war {dev_pct:.2f}% abweichend)", logger)
        if not dry_run:
            _send_sync_chart(telegram_config, fix_chart_path,
                            f"{symbol} ({tf}): korrigiert", logger)
        elif fix_chart_path:
            print(f"[DRY-RUN] Korrektur-Chart erzeugt: {fix_chart_path}")

    logger.info(f"--- Brick-Sync-Check abgeschlossen ---")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                       help='Nur pruefen/loggen, kein Telegram, keine Datei-Korrektur.')
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
