# src/zerobot/analysis/oos_tester.py
# Walk-Forward Out-of-Sample Test
#
# Simulates live bot EXACTLY on the "dark zone" (data optimizer never saw):
#   1. Download full data: [warmup_start .. oos_end] (all upfront, for speed)
#   2. Build EAR-bricks and signals on full period (CAUSAL — no lookahead)
#   3. Enter trades ONLY from oos_start onwards (warmup builds brick state)
#   4. Report OOS performance vs optimizer's in-sample promise

import os
import sys
import json
import argparse
import warnings
from datetime import datetime as _dt, timedelta

warnings.filterwarnings('ignore')

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.analysis.backtester import load_data, run_backtest
from zerobot.utils.timeframe_utils import determine_htf

CONFIGS_DIR  = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
RESULTS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_optimizer_run.json')
OOS_FILE     = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')

GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
RED    = '\033[0;31m'
CYAN   = '\033[0;36m'
BOLD   = '\033[1m'
NC     = '\033[0m'


def load_config(filename: str) -> dict:
    path = os.path.join(CONFIGS_DIR, filename)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def run_oos_test(oos_start: str, oos_end: str, warmup_start: str,
                 start_capital: float, configs_filter: list = None) -> list:
    """
    Runs walk-forward OOS test for all (or filtered) configs.
    Returns list of result dicts.
    """
    # In-Sample PnL aus last_optimizer_run.json (nur für Anzeige)
    insample_map = {}
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE) as f:
                opt_results = json.load(f)
            for r in opt_results.get('saved', []):
                insample_map[r['config_file']] = r.get('pnl_pct', 0)
        except Exception:
            pass

    # Immer ALLE Configs aus dem Verzeichnis lesen (nicht nur last run)
    saved_configs = []
    if os.path.isdir(CONFIGS_DIR):
        for fn in sorted(os.listdir(CONFIGS_DIR)):
            if fn.startswith('config_') and fn.endswith('.json'):
                cfg = load_config(fn)
                m   = cfg.get('market', {})
                # In-Sample PnL: aus last_optimizer_run.json, sonst aus _meta
                in_pnl = insample_map.get(fn, cfg.get('_meta', {}).get('pnl_pct', 0))
                saved_configs.append({
                    'config_file': fn,
                    'symbol':      m.get('symbol', '?'),
                    'timeframe':   m.get('timeframe', '?'),
                    'pnl_pct':     in_pnl,
                })

    if configs_filter:
        saved_configs = [c for c in saved_configs
                         if c['config_file'] in configs_filter]

    results = []

    for entry in saved_configs:
        cfg_file  = entry.get('config_file', '')
        symbol    = entry.get('symbol', '')
        timeframe = entry.get('timeframe', '')
        in_pnl    = entry.get('pnl_pct', 0)

        if not cfg_file or not symbol or not timeframe:
            continue

        cfg           = load_config(cfg_file)
        strategy_params = cfg.get('strategy', {})
        risk_params     = cfg.get('risk', {})

        print(f"\n  {BOLD}OOS-Test: {symbol} ({timeframe}){NC}")
        print(f"    In-Sample PnL (Optimizer): {in_pnl:+.1f}%")

        # Warmup-Start aus Config-Metadaten — exakt der Zeitraum den der Optimizer verwendet hat
        cfg_full      = load_config(cfg_file)
        actual_warmup = cfg_full.get('_meta', {}).get('train_start', warmup_start)

        print(f"    Lade Daten: {actual_warmup} → {oos_end} ...")
        full_data = load_data(symbol, timeframe, actual_warmup, oos_end)
        if full_data.empty or len(full_data) < 50:
            print(f"    {RED}Keine Daten verfügbar — überspringe.{NC}")
            results.append({
                'symbol': symbol, 'timeframe': timeframe,
                'config_file': cfg_file,
                'in_sample_pnl': in_pnl,
                'oos_pnl': None, 'error': 'no_data',
            })
            continue

        print(f"    {len(full_data)} Kerzen geladen. Starte Walk-Forward OOS-Simulation ...")

        # run_backtest with trade_start_date = oos_start
        # All data before oos_start = warmup (bricks build up state, no trades)
        oos_result = run_backtest(
            full_data.copy(),
            strategy_params,
            risk_params,
            start_capital=start_capital,
            return_trades=True,
            trade_start_date=oos_start,
        )

        oos_pnl      = oos_result.get('total_pnl_pct', -100)
        oos_trades   = oos_result.get('trades_count', 0)
        oos_wr       = oos_result.get('win_rate', 0)
        oos_dd       = oos_result.get('max_drawdown_pct', 0) * 100
        oos_capital  = oos_result.get('end_capital', start_capital)

        # Prozentuale Returns pro Trade (fuer Monte Carlo)
        raw_trades = oos_result.get('trades', [])
        pnl_pcts   = []
        for t in raw_trades:
            cap_after  = t.get('capital_after', start_capital)
            pnl_usd    = t.get('pnl_usd', 0.0)
            cap_before = cap_after - pnl_usd
            if cap_before > 0:
                pnl_pcts.append(round(pnl_usd / cap_before, 8))

        # Verdict
        if oos_pnl > 0 and oos_dd < 30:
            verdict = f"{GREEN}✅ Robust{NC}"
        elif oos_pnl > -10:
            verdict = f"{YELLOW}⚠  Grenzwertig{NC}"
        else:
            verdict = f"{RED}❌ Overfitted{NC}"

        print(f"    OOS PnL:     {oos_pnl:+.1f}%  (vs. In-Sample: {in_pnl:+.1f}%)")
        print(f"    OOS Trades:  {oos_trades}  |  WR: {oos_wr:.1f}%  |  MaxDD: {oos_dd:.1f}%")
        print(f"    Endkapital:  {oos_capital:.2f} USDT  (Start: {start_capital:.2f})")
        print(f"    Bewertung:   {verdict}")

        results.append({
            'symbol':        symbol,
            'timeframe':     timeframe,
            'config_file':   cfg_file,
            'in_sample_pnl': round(in_pnl, 2),
            'oos_pnl':       round(oos_pnl, 2),
            'oos_trades':    oos_trades,
            'oos_win_rate':  round(oos_wr, 2),
            'oos_max_dd':    round(oos_dd, 2),
            'oos_capital':   round(oos_capital, 4),
            'oos_pnl_pcts':  pnl_pcts,
        })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Walk-Forward OOS Test für ZeroBot EAR Strategien")
    parser.add_argument('--oos_start',    required=True, type=str,
                        help='OOS Startdatum (YYYY-MM-DD) — erster Handelstag im dunklen Bereich')
    parser.add_argument('--oos_end',      required=True, type=str,
                        help='OOS Enddatum (YYYY-MM-DD) — letzter Handelstag')
    parser.add_argument('--warmup_start', required=True, type=str,
                        help='Warmup-Startdatum (YYYY-MM-DD) — Anfang der Trainingsdaten')
    parser.add_argument('--start_capital', type=float, default=100.0,
                        help='Startkapital in USDT [Standard: 100]')
    args = parser.parse_args()

    print(f"\n{'=' * 65}")
    print(f"  WALK-FORWARD OUT-OF-SAMPLE TEST (ZeroBot EAR)")
    print(f"{'=' * 65}")
    print(f"  Warmup-Periode:   {args.warmup_start}  →  {args.oos_start} (Brick-Aufbau)")
    print(f"  OOS-Periode:      {args.oos_start}  →  {args.oos_end}   (DUNKLER BEREICH)")
    print(f"  Startkapital:     {args.start_capital} USDT")
    print(f"  Modus:            Exakte Live-Bot-Simulation (kein Lookahead)")
    print(f"{'=' * 65}")

    results = run_oos_test(
        oos_start      = args.oos_start,
        oos_end        = args.oos_end,
        warmup_start   = args.warmup_start,
        start_capital  = args.start_capital,
    )

    if not results:
        print(f"\n{RED}Keine Configs gefunden — zuerst run_pipeline.sh ausführen.{NC}")
        return

    # Zusammenfassung
    print(f"\n{'=' * 65}")
    print(f"  ZUSAMMENFASSUNG OOS-TEST")
    print(f"{'=' * 65}")
    print(f"  {'Symbol/TF':<28} {'In-Sample':>10} {'OOS PnL':>10} {'WR':>7} {'DD':>7}")
    print(f"  {'-' * 63}")

    robust = overfitted = marginal = 0
    for r in results:
        if r.get('oos_pnl') is None:
            print(f"  {r['symbol']} ({r['timeframe']})  →  KEINE DATEN")
            continue
        oos    = r['oos_pnl']
        ins    = r['in_sample_pnl']
        wr     = r['oos_win_rate']
        dd     = r['oos_max_dd']
        label  = f"{r['symbol']} ({r['timeframe']})"
        color  = GREEN if oos > 0 and dd < 30 else (YELLOW if oos > -10 else RED)
        print(f"  {label:<28} {ins:>+9.1f}% {color}{oos:>+9.1f}%{NC} {wr:>6.1f}% {dd:>6.1f}%")
        if oos > 0 and dd < 30:    robust     += 1
        elif oos > -10:            marginal   += 1
        else:                      overfitted += 1

    print(f"  {'-' * 63}")
    print(f"  Robust: {robust}  |  Grenzwertig: {marginal}  |  Overfitted: {overfitted}")
    print(f"{'=' * 65}\n")

    # Ergebnisse speichern
    os.makedirs(os.path.dirname(OOS_FILE), exist_ok=True)
    with open(OOS_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'run_date':     _dt.now().isoformat(timespec='seconds'),
            'oos_start':    args.oos_start,
            'oos_end':      args.oos_end,
            'warmup_start': args.warmup_start,
            'start_capital': args.start_capital,
            'results':      results,
        }, f, indent=2, ensure_ascii=False)
    print(f"  Ergebnisse gespeichert: {OOS_FILE}")


if __name__ == '__main__':
    main()
