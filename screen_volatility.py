#!/usr/bin/env python3
"""
screen_volatility.py

Schnelle Vorfilterung OHNE Optuna/Backtest: berechnet fuer jedes Kandidaten-
Symbol/Timeframe reine EAR-Brick-Kennzahlen (ueber die ECHTE EAREngine, keine
eigene Naeherung -- siehe feedback_live_backtest_must_match) und vergleicht
sie mit denselben Kennzahlen der aktuell aktiven, bestaetigten Strategien
(der "bekannt gute" Referenz-Cluster). Kandidaten, die diesem Profil aehneln,
sind wahrscheinlicher gute EAR-Kandidaten -- OHNE dass dafuer ein einziger
Optuna-Trial/Backtest laufen muss. Braucht nur OHLCV-Daten + die echte
_build_bricks()/process_dataframe()-Logik, daher Minuten statt Stunden fuer
hunderte Symbole.

Kennzahlen (aus der echten EAREngine, kein Nachbau):
  - bricks_per_week: wie viele Bricks pro Woche entstehen (Aktivitaet)
  - avg_H: durchschnittliche geglaettete Entropie der Bricks (Chaos vs. Ordnung)
  - streak_ge_min_pct: Anteil der gleichgerichteten Brick-Serien, die die
    trend_min_bricks-Schwelle erreichen (wie oft entsteht ueberhaupt ein
    potenzielles Signal statt staendigem Richtungswechsel)
  - atr_pct: durchschnittliche ATR als % vom Preis (Volatilitaet)
  - signals_per_week: EXAKTE Signalzahl aus process_dataframe() (derselben
    Funktion, die Backtester und Live-Bot fuer echte Signale nutzen)

Danach: Distanz jedes Kandidaten zum Median-Profil der Referenz-Strategien
(z-normalisiert je Kennzahl) -- kleinste Distanz = aehnlichstes Profil = mit
hoher Prioritaet fuer die (teure) volle Optuna-Pipeline (run_pipeline.sh).

Aufruf:
  python screen_volatility.py                     # alle aktiven USDT-Perpetuals
  python screen_volatility.py --top-n 200          # nur Top 200 nach Volumen
  python screen_volatility.py --timeframes "1h 4h" # andere Timeframes
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd
import ta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange  # noqa: E402
from zerobot.analysis.backtester import load_data  # noqa: E402
from zerobot.strategy.ear_engine import EAREngine  # noqa: E402

DEFAULT_TIMEFRAMES = ['1h', '2h', '4h', '6h']
CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
CSV_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'screen_volatility.csv')

METRIC_COLS = ['bricks_per_week', 'avg_H', 'streak_ge_min_pct', 'atr_pct', 'signals_per_week']

# Lookback-Tage je Timeframe aus run_pipeline.sh's Empfehlungstabelle -- ein
# Kandidat mit weniger tatsaechlich verfuegbarer Historie als das wuerde die
# volle Optuna-Pipeline nur mit "Keine historischen OHLCV-Daten gefunden"
# verschwenden (siehe ltbbots screen_volatility.py, gleiches Muster).
PIPELINE_LOOKBACK_DAYS = {
    '15m': 180, '30m': 180,
    '1h':  548,
    '2h':  730,
    '4h': 1095, '6h': 1095,
    '1d': 1825,
}


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def compute_ear_stats(df: pd.DataFrame, base_pct: float, k_entropy: float,
                       h_window: int, trend_min_bricks: int) -> dict | None:
    """Reine EAR-Brick-Kennzahlen aus OHLCV -- ueber die echte EAREngine
    (_build_bricks + process_dataframe), keine eigene Signal-Naeherung."""
    if df is None or len(df) < max(h_window, 50) + 20:
        return None

    df = df.copy()
    atr_ind = ta.volatility.AverageTrueRange(high=df['high'], low=df['low'],
                                              close=df['close'], window=14)
    df['atr'] = atr_ind.average_true_range()
    df = df.dropna(subset=['atr'])
    if len(df) < 50:
        return None

    settings = {'base_pct': base_pct, 'k_entropy': k_entropy,
                'h_window': h_window, 'trend_min_bricks': trend_min_bricks}
    engine = EAREngine(settings=settings)
    bricks = engine._build_bricks(df)
    if not bricks:
        return None

    span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400.0
    weeks = max(span_days / 7.0, 1.0)

    streaks = []
    cur_dir, cur_len = bricks[0]['direction'], 1
    for b in bricks[1:]:
        if b['direction'] == cur_dir:
            cur_len += 1
        else:
            streaks.append(cur_len)
            cur_dir, cur_len = b['direction'], 1
    streaks.append(cur_len)

    # Exakte Signalzahl aus derselben Funktion, die Backtester/Live-Bot nutzen.
    sig_df = engine.process_dataframe(df)
    n_signals = int((sig_df['ear_signal'] != 0).sum())

    return {
        'bricks_per_week':   round(len(bricks) / weeks, 2),
        'avg_H':             round(float(np.mean([b['H'] for b in bricks])), 4),
        'streak_ge_min_pct': round(sum(1 for s in streaks if s >= trend_min_bricks)
                                    / len(streaks) * 100, 2),
        'atr_pct':           round(float((df['atr'] / df['close']).mean() * 100), 4),
        'signals_per_week':  round(n_signals / weeks, 3),
        'history_days':      round(span_days, 1),
        'n_candles':         len(df),
    }


def has_sufficient_history(symbol: str, timeframe: str, secrets: dict, required_days: int) -> bool | None:
    """Prueft NICHT das genaue Listing-Datum (ccxt's einfaches fetch_ohlcv mit
    altem `since` liefert bei Bitget ab ~300-400 Tagen Rueckstand leer -- ein
    API-Limit der einfachen Methode, keine echte Datengrenze, siehe ltbbots
    identisches Vorgehen). Stattdessen: gezielte Anfrage per
    fetch_historical_ohlcv() (dieselbe paginierte Methode wie der volle
    Download) fuer ein schmales 2-Tage-Fenster GENAU am benoetigten
    Schwellenwert -- liefert das Kerzen, existierte das Symbol bereits.
    Eigenes Exchange-Objekt pro Aufruf (thread-sicher, siehe load_data())."""
    try:
        ex = Exchange(secrets['zerobot'][0])
        target = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=required_days)
        start = target.strftime('%Y-%m-%d')
        end = (target + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
        df = ex.fetch_historical_ohlcv(symbol, timeframe, start, end)
        return df is not None and not df.empty
    except Exception:
        return None


def build_reference_profile(lookback_weeks: int) -> pd.DataFrame:
    """Kennzahlen der aktuell aktiven Strategien, jeweils mit ihren EIGENEN
    EAR-Parametern aus der echten Config -- das ist der 'bekannt gute'
    Vergleichs-Cluster."""
    with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
        settings = json.load(f)
    active = [s for s in settings.get('live_trading_settings', {}).get('active_strategies', [])
              if s.get('active')]

    end_date = date.today().strftime('%Y-%m-%d')
    start_date = (date.today() - timedelta(weeks=lookback_weeks)).strftime('%Y-%m-%d')

    rows = []
    for s in active:
        symbol, timeframe = s['symbol'], s['timeframe']
        coin = symbol.split('/')[0]
        cfg_path = os.path.join(CONFIGS_DIR, f"config_{coin}USDTUSDT_{timeframe}.json")
        if not os.path.exists(cfg_path):
            continue
        with open(cfg_path) as f:
            cfg = json.load(f)
        strat = cfg['strategy']
        df = load_data(symbol, timeframe, start_date, end_date)
        stats = compute_ear_stats(df, strat['base_pct'], strat['k_entropy'],
                                   strat['h_window'], strat['trend_min_bricks'])
        if stats:
            stats.update({'symbol': symbol, 'timeframe': timeframe, **strat})
            rows.append(stats)
            _log(f"Referenz {symbol} ({timeframe}): {stats}")
    return pd.DataFrame(rows)


def rank_candidates(ref_df: pd.DataFrame, cand_df: pd.DataFrame) -> pd.DataFrame:
    """Z-normalisierte euklidische Distanz jedes Kandidaten zum Median-Profil
    der Referenz-Strategien -- kleinste Distanz = aehnlichstes Profil."""
    ref_median = ref_df[METRIC_COLS].median()
    ref_std = ref_df[METRIC_COLS].std().replace(0, 1.0)

    z = (cand_df[METRIC_COLS] - ref_median) / ref_std
    cand_df = cand_df.copy()
    cand_df['fit_distance'] = np.sqrt((z ** 2).sum(axis=1))
    return cand_df.sort_values('fit_distance')


def main():
    parser = argparse.ArgumentParser(description="Schnelles EAR-Brick-Screening (kein Optuna)")
    parser.add_argument('--top-n', type=int, default=None, help='Nur Top N nach 24h-Volumen (Default: alle aktiven)')
    parser.add_argument('--timeframes', type=str, default=' '.join(DEFAULT_TIMEFRAMES))
    parser.add_argument('--lookback-weeks', type=int, default=16)
    parser.add_argument('--workers', type=int, default=8, help='Parallele Threads fuer Datenabruf (I/O-gebunden)')
    args = parser.parse_args()
    timeframes = args.timeframes.split()

    with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
        secrets = json.load(f)
    ex = Exchange(secrets['zerobot'][0])

    _log("Baue Referenz-Profil aus den aktiven Strategien...")
    ref_df = build_reference_profile(args.lookback_weeks)
    if ref_df.empty:
        _log("FEHLER: Kein Referenz-Profil berechenbar (keine aktiven Configs/Daten gefunden).")
        return
    _log(f"Referenz-Median: {ref_df[METRIC_COLS].median().to_dict()}")

    tickers = ex.exchange.fetch_tickers(params={'productType': 'USDT-FUTURES'})
    active_symbols = {
        m['symbol'] for m in ex.markets.values()
        if m.get('swap') and m.get('quote') == 'USDT' and m.get('settle') == 'USDT' and m.get('active', True)
    }
    vol_rows = [(sym, t.get('quoteVolume') or 0.0) for sym, t in tickers.items() if sym in active_symbols]
    vol_rows.sort(key=lambda r: r[1], reverse=True)
    symbols = [s for s, _ in vol_rows]
    if args.top_n:
        symbols = symbols[:args.top_n]
    _log(f"{len(symbols)} Kandidaten-Symbole x {len(timeframes)} Timeframes = "
         f"{len(symbols)*len(timeframes)} Kombinationen.")

    end_date = date.today().strftime('%Y-%m-%d')
    start_date = (date.today() - timedelta(weeks=args.lookback_weeks)).strftime('%Y-%m-%d')
    # Generischer Default fuer Kandidaten (die haben ja noch keine eigene
    # Config) -- Median der aktiven Referenz-Strategien, statt frei erfunden.
    generic_base_pct         = float(ref_df['base_pct'].median())
    generic_k_entropy        = float(ref_df['k_entropy'].median())
    generic_h_window         = int(round(ref_df['h_window'].median()))
    generic_trend_min_bricks = int(round(ref_df['trend_min_bricks'].median()))
    _log(f"Generische Kandidaten-Parameter: base_pct={generic_base_pct}, "
         f"k_entropy={generic_k_entropy}, h_window={generic_h_window}, "
         f"trend_min_bricks={generic_trend_min_bricks}")

    def _process_one(symbol_tf):
        symbol, tf = symbol_tf
        try:
            df = load_data(symbol, tf, start_date, end_date)
            stats = compute_ear_stats(df, generic_base_pct, generic_k_entropy,
                                       generic_h_window, generic_trend_min_bricks)
            if stats:
                required = PIPELINE_LOOKBACK_DAYS.get(tf, 730)
                sufficient = has_sufficient_history(symbol, tf, secrets, required)
                stats.update({
                    'symbol': symbol, 'timeframe': tf,
                    'required_history_days': required,
                    'sufficient_history': sufficient,
                })
                return stats
        except Exception as e:
            _log(f"  {symbol} ({tf}): Fehler {e}")
        return None

    tasks = [(sym, tf) for sym in symbols for tf in timeframes]
    rows = []
    t0 = time.time()
    done = 0
    # I/O-gebunden (Netzwerk-Fetches) -- parallele Threads bringen hier einen
    # echten Speedup, da load_data() pro Aufruf ein eigenes Exchange-Objekt
    # anlegt und verschiedene Symbol/Timeframe-Caches nie denselben Pfad
    # treffen, also nichts kollidiert.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_process_one, t): t for t in tasks}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                rows.append(result)
            done += 1
            if done % 50 == 0 or done == len(tasks):
                elapsed = time.time() - t0
                _log(f"[{done}/{len(tasks)}] Kombinationen verarbeitet "
                     f"({elapsed:.0f}s, {elapsed/done:.2f}s/Kombo, {args.workers} Worker)")

    if not rows:
        _log("Keine Kandidaten-Daten berechnet.")
        return
    cand_df = pd.DataFrame(rows)
    ranked = rank_candidates(ref_df, cand_df)

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    ranked.to_csv(CSV_PATH, index=False)

    n_too_new = int((ranked['sufficient_history'] == False).sum())  # noqa: E712
    ranked_ok = ranked[ranked['sufficient_history'] != False]  # noqa: E712 (True oder None/unbekannt durchlassen)

    def _hist_flag(r):
        if r['sufficient_history'] is True:
            return 'OK'
        if r['sufficient_history'] is False:
            return f"ZU NEU (<{r['required_history_days']:.0f}d)"
        return '?'

    print(f"\n{'='*110}")
    print(f"  Top 30 nach Aehnlichkeit zum Referenz-Profil (kleinste fit_distance zuerst)")
    print(f"  {n_too_new} Kombination(en) wegen zu kurzer Historie fuer die volle Pipeline ausgeblendet "
          f"(siehe screen_volatility.csv fuer die volle Liste inkl. dieser).")
    print(f"{'='*110}")
    print(f"  {'Symbol':<14}{'TF':<6}{'Bricks/Wo':<11}{'avg_H':<8}{'Streak>=N%':<12}{'ATR%':<8}{'Sig/Wo':<9}{'Hist':<14}{'Distanz':<8}")
    for _, r in ranked_ok.head(30).iterrows():
        print(f"  {r['symbol']:<14}{r['timeframe']:<6}{r['bricks_per_week']:<11}{r['avg_H']:<8}"
              f"{r['streak_ge_min_pct']:<12}{r['atr_pct']:<8}{r['signals_per_week']:<9}{_hist_flag(r):<14}{r['fit_distance']:.3f}")
    print(f"{'='*110}")
    print(f"  Referenz-Median (aktive Strategien): {ref_df[METRIC_COLS].median().to_dict()}")
    print(f"  Volle Ergebnisliste: {CSV_PATH}")


if __name__ == '__main__':
    main()
