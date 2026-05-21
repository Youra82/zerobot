# src/zerobot/analysis/show_results.py
# Quantum State Analyse — 4 Modi
#
# Modus 1: State Bibliothek     — Top-Muster + Backtest-Ergebnisse
# Modus 2: Regime-Analyse       — Welches Regime funktioniert wo am besten
# Modus 3: Decay-Status         — Welche States altern / drohen zu deaktivieren
# Modus 4: Interaktiver Chart   — Candlestick + Entry/Exit-Marker + Equity-Kurve
#
# Ausführung über show_results.sh oder direkt:
#   python3 src/zerobot/analysis/show_results.py --mode 1
#   python3 src/zerobot/analysis/show_results.py --mode 2 --symbol BTC/USDT:USDT --timeframe 4h

import os
import sys
import json
import glob
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.physics.database import StateDB

RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')
DB_PATH     = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'quantum.db')

G   = '\033[0;32m'
B   = '\033[0;34m'
Y   = '\033[1;33m'
R   = '\033[0;31m'
C   = '\033[0;36m'
DIM = '\033[2m'
NC  = '\033[0m'

SEP  = '=' * 72
SEP2 = '-' * 72

REGIMES = ['TREND', 'REVERTING', 'NEUTRAL']
_REGIME_OCC = {
    'TREND':     'occ_trend',
    'REVERTING': 'occ_range',
    'NEUTRAL':   'occ_neutral',
}
_REGIME_WINS = {
    'TREND':     'wins_trend',
    'REVERTING': 'wins_range',
    'NEUTRAL':   'wins_neutral',
}


def _load_db() -> StateDB | None:
    if not os.path.exists(DB_PATH):
        print(f"{R}Quantum-Datenbank nicht gefunden. Erst scan_and_learn.py ausführen.{NC}")
        return None
    return StateDB(DB_PATH)


def _age_days(iso: str | None) -> float:
    if not iso:
        return 9999.0
    try:
        ts = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return 9999.0


def _regime_list(state: dict) -> list:
    try:
        return json.loads(state.get('active_regimes', '[]'))
    except (json.JSONDecodeError, TypeError):
        return []


def _winrate(s: dict) -> float:
    return s['wins'] / max(s['total_occurrences'], 1)


def _regime_wr(s: dict, regime: str) -> tuple[float, int]:
    occ  = s.get(_REGIME_OCC.get(regime, 'occ_neutral'), 0) or 0
    wins = s.get(_REGIME_WINS.get(regime, 'wins_neutral'), 0) or 0
    if occ == 0:
        return 0.0, 0
    return wins / occ, occ


# ─────────────────────────────────────────────────────────────────────────────
# Modus 1: State Bibliothek Übersicht
# ─────────────────────────────────────────────────────────────────────────────

def mode_overview(db: StateDB):
    summary = db.get_db_summary()

    print(f"\n{SEP}")
    print(f"  {Y}QUANTUM STATE LIBRARY — ÜBERSICHT{NC}")
    print(SEP)
    print(f"  Gesamt-States:    {summary['total_states']}")
    print(f"  Aktive States:    {G}{summary['active_states']}{NC}")
    print(f"  Märkte in DB:     {', '.join(summary['markets']) or '—'}")

    # Per-Markt Zusammenfassung
    pairs = db.get_all_market_pairs()
    if pairs:
        print()
        print(f"  {'Markt':<22} {'TF':<5} {'Gesamt':>7} {'Aktiv':>6} {'Ø Score':>8} {'Ø WR':>7}")
        print(f"  {SEP2}")
        for market, tf in pairs:
            all_states    = db.get_all_states(market, tf)
            active_states = [s for s in all_states if s['active']]
            total   = len(all_states)
            active  = len(active_states)
            avg_score = (sum(s['score'] for s in active_states) / active) if active else 0.0
            avg_wr    = (sum(_winrate(s) for s in active_states) / active) if active else 0.0
            print(
                f"  {market:<22} {tf:<5} {total:>7} "
                f"{G}{active:>6}{NC} {avg_score:>8.3f} {avg_wr:>6.1%}"
            )

    # Top 10 global
    top = summary['top_patterns']
    if top:
        print()
        print(f"  {Y}Top 10 Muster (global, aktiv){NC}")
        print(f"  {SEP2}")
        for i, p in enumerate(top, 1):
            regimes = _regime_list(p)
            wr = p.get('winrate', 0)
            hc = p.get('hurst_count', 0) or 0
            ac = p.get('apen_count', 0) or 0
            avg_h = (p.get('hurst_sum', 0) / hc) if hc > 0 else 0.5
            avg_a = (p.get('apen_sum', 0) / ac) if ac > 0 else 1.0
            print(
                f"  {i:2}. [{p['direction']:<5}] Score {G}{p['score']:.3f}{NC} | "
                f"WR {wr:.1%} | Avg Move {p.get('avg_move_pct', 0):.2f}% | "
                f"n={p['total_occurrences']}"
            )
            print(
                f"      {DIM}{p['sequence']}{NC}  "
                f"Hurst: {avg_h:.3f} | ApEn: {avg_a:.3f} | "
                f"Regime: {', '.join(regimes) or '—'} | {p['market']} {p['timeframe']}"
            )

    # Backtest-Ergebnisse
    result_files = glob.glob(os.path.join(RESULTS_DIR, 'backtest_*.json'))
    if result_files:
        print()
        print(f"  {Y}Backtest-Ergebnisse (TEST-Periode bevorzugt){NC}")
        print(f"  {SEP2}")
        print(f"  {'Markt':<22} {'TF':<5} {'Trades':>7} {'WR':>7} {'PnL%':>8} {'PF':>6} {'MaxDD':>7}")

        # Prefer _test over _train for same (market, tf)
        seen: dict = {}
        for path in sorted(result_files):
            try:
                with open(path) as f:
                    data = json.load(f)
                raw_tf  = data.get('timeframe', '')
                market  = data.get('market', '?')
                is_test = raw_tf.endswith('_test')
                base_tf = raw_tf.removesuffix('_test').removesuffix('_train')
                key = (market, base_tf)
                st  = data.get('stats', {})
                if st.get('total_trades', 0) == 0:
                    continue
                entry = {'market': market, 'tf': base_tf, 'stats': st, 'is_test': is_test}
                if key not in seen or (is_test and not seen[key]['is_test']):
                    seen[key] = entry
            except Exception:
                pass

        for s in sorted(seen.values(), key=lambda x: x['stats'].get('total_pnl_pct', 0), reverse=True):
            st   = s['stats']
            sign = '+' if st.get('total_pnl_pct', 0) >= 0 else ''
            col  = G if st.get('total_pnl_pct', 0) >= 0 else R
            pf   = st.get('profit_factor', 0)
            pf_str = f"{pf:.2f}" if pf != float('inf') else '∞'
            print(
                f"  {s['market']:<22} {s['tf']:<5} {st.get('total_trades', 0):>7} "
                f"{st.get('win_rate', 0):>6.1%} "
                f"{col}{sign}{st.get('total_pnl_pct', 0):>7.1f}%{NC} "
                f"{pf_str:>6} {st.get('max_drawdown_pct', 0):>6.1f}%"
            )

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Modus 1b: Symbol-Detail (via --symbol + --timeframe)
# ─────────────────────────────────────────────────────────────────────────────

def mode_symbol_detail(db: StateDB, symbol: str, timeframe: str, top_n: int = 20):
    all_s = db.get_all_states(symbol, timeframe)
    if not all_s:
        print(f"{R}Keine States für {symbol} ({timeframe}) gefunden.{NC}")
        return

    active   = [s for s in all_s if s['active']]
    inactive = [s for s in all_s if not s['active']]

    print(f"\n{SEP}")
    print(f"  {Y}SYMBOL-DETAIL: {symbol} ({timeframe}){NC}")
    print(SEP)
    print(f"  Gesamt: {len(all_s)} | Aktiv: {G}{len(active)}{NC} | Inaktiv: {DIM}{len(inactive)}{NC}")

    if not active:
        print(f"\n  {R}Keine aktiven States. scan_and_learn.py ausführen.{NC}\n")
        print(f"{SEP}\n")
        return

    sorted_s = sorted(active, key=lambda x: x['score'], reverse=True)[:top_n]

    print()
    print(f"  {'#':<3} {'Dir':<6} {'Score':>7} {'WR':>7} {'Avg%':>7} {'n':>5}  Regime (WR/occ)")
    print(f"  {SEP2}")

    for i, s in enumerate(sorted_s, 1):
        regimes = _regime_list(s)
        wr = _winrate(s)

        regime_parts = []
        for reg in REGIMES:
            rwr, rocc = _regime_wr(s, reg)
            if rocc > 0:
                active_marker = G + '●' + NC if reg in regimes else DIM + '○' + NC
                regime_parts.append(f"{active_marker}{reg[:3]} {rwr:.0%}/{rocc}")
        regime_str = '  '.join(regime_parts) if regime_parts else '—'

        dir_col = G if s['direction'] == 'LONG' else R
        hc = s.get('hurst_count', 0) or 0
        ac = s.get('apen_count', 0) or 0
        avg_h = (s.get('hurst_sum', 0) / hc) if hc > 0 else 0.5
        avg_a = (s.get('apen_sum', 0) / ac) if ac > 0 else 1.0
        print(
            f"  {i:<3} {dir_col}{s['direction']:<6}{NC} "
            f"{s['score']:>7.3f} {wr:>6.1%} "
            f"{s.get('avg_move_pct', 0):>6.2f}% {s['total_occurrences']:>5}  "
            f"{regime_str}"
        )
        print(
            f"      {DIM}{s['sequence']}{NC}  "
            f"Hurst: {avg_h:.3f} | ApEn: {avg_a:.3f} | "
            f"last: {(s.get('last_seen', '') or '')[:10] or '—'}"
        )

    longs  = sum(1 for s in active if s['direction'] == 'LONG')
    shorts = sum(1 for s in active if s['direction'] == 'SHORT')
    print()
    print(f"  Richtungsverteilung:  {G}LONG {longs}{NC}  /  {R}SHORT {shorts}{NC}")

    by_len: dict[int, int] = {}
    for s in active:
        by_len[s['seq_length']] = by_len.get(s['seq_length'], 0) + 1
    len_str = '  '.join(f"{l}er: {c}" for l, c in sorted(by_len.items()))
    print(f"  Sequenzlängen:        {len_str}")
    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Modus 2: Regime-Analyse
# ─────────────────────────────────────────────────────────────────────────────

def mode_regime_analysis(db: StateDB):
    all_s = db.get_all_states()

    print(f"\n{SEP}")
    print(f"  {Y}REGIME-ANALYSE — Alle Märkte{NC}")
    print(SEP)

    if not all_s:
        print(f"  {R}Datenbank leer.{NC}\n")
        return

    active = [s for s in all_s if s['active']]
    print(f"  Aktive States gesamt: {len(active)}")
    print()

    print(f"  {Y}Global (alle Märkte){NC}")
    print(f"  {'Regime':<12} {'Aktive':>7} {'Ø WR':>7} {'Ø occ':>7}")
    print(f"  {SEP2}")
    for reg in REGIMES:
        with_data = [s for s in active if s.get(_REGIME_OCC.get(reg, 'occ_neutral'), 0) > 0]
        if not with_data:
            print(f"  {reg:<12} {'—':>7}")
            continue
        avg_wr  = sum(_regime_wr(s, reg)[0] for s in with_data) / len(with_data)
        avg_occ = sum(_regime_wr(s, reg)[1] for s in with_data) / len(with_data)
        n_in_regime = sum(1 for s in active if reg in _regime_list(s))
        print(f"  {reg:<12} {n_in_regime:>7} {avg_wr:>6.1%} {avg_occ:>7.0f}")

    by_pair: dict[tuple, list] = {}
    for s in active:
        key = (s['market'], s['timeframe'])
        by_pair.setdefault(key, []).append(s)

    print()
    print(f"  {Y}Per Markt{NC}")
    header = f"  {'Markt':<22} {'TF':<5}"
    for reg in REGIMES:
        header += f"  {reg:<17}"
    print(header)
    print(f"  {SEP2}")

    for (mkt, tf), gs in sorted(by_pair.items()):
        row = f"  {mkt:<22} {tf:<5}"
        for reg in REGIMES:
            with_data = [s for s in gs if s.get(_REGIME_OCC.get(reg, 'occ_neutral'), 0) > 0]
            if not with_data:
                row += f"  {'—':<17}"
            else:
                avg_wr = sum(_regime_wr(s, reg)[0] for s in with_data) / len(with_data)
                n      = sum(1 for s in gs if reg in _regime_list(s))
                col    = G if n > 0 else DIM
                row += f"  {col}{n} aktiv / {avg_wr:.0%}{NC:<17}"
        print(row)

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Modus 3: Decay-Status
# ─────────────────────────────────────────────────────────────────────────────

def mode_decay_status(db: StateDB):
    all_s  = db.get_all_states()
    active = [s for s in all_s if s['active']]

    print(f"\n{SEP}")
    print(f"  {Y}DECAY-STATUS — States nach Alter (last_seen){NC}")
    print(SEP)

    if not active:
        print(f"  {R}Keine aktiven States.{NC}\n")
        return

    for s in active:
        s['_age_days'] = _age_days(s.get('last_seen'))

    sorted_s = sorted(active, key=lambda x: x['_age_days'], reverse=True)

    critical = [s for s in sorted_s if s['_age_days'] > 180]
    warning  = [s for s in sorted_s if 90 < s['_age_days'] <= 180]
    fresh    = [s for s in sorted_s if s['_age_days'] <= 90]

    def _age_str(days: float) -> str:
        if days > 9000:
            return 'nie gesehen'
        if days >= 365:
            return f"{days/365:.1f} Jahre"
        return f"{days:.0f} Tage"

    print(
        f"  {G}Frisch (≤90d):{NC}   {len(fresh):>4}  |  "
        f"  {Y}Warnung (90–180d):{NC} {len(warning):>4}  |  "
        f"  {R}Kritisch (>180d):{NC}  {len(critical):>4}"
    )

    for label, group, col in [
        ('KRITISCH', critical, R),
        ('WARNUNG',  warning,  Y),
        ('FRISCH',   fresh,    G),
    ]:
        if not group:
            continue
        print()
        print(f"  {col}── {label} ({len(group)} States) ──{NC}")
        print(f"  {'Markt':<22} {'TF':<5} {'Dir':<6} {'Alter':>12} {'Score':>7} {'Sequenz'}")
        print(f"  {SEP2}")
        for s in group[:25]:
            print(
                f"  {s['market']:<22} {s['timeframe']:<5} "
                f"{s['direction']:<6} {_age_str(s['_age_days']):>12} "
                f"{s['score']:>7.3f}  {DIM}{s['sequence'][:50]}{NC}"
            )
        if len(group) > 25:
            print(f"  {DIM}  ... und {len(group) - 25} weitere{NC}")

    if critical:
        print()
        print(f"  {Y}Empfehlung:{NC} {len(critical)} States wurden >180 Tage nicht mehr beobachtet.")
        print(f"  Ihr effektives Gewicht ist stark reduziert. scan_and_learn.py ausführen,")
        print(f"  um sie mit aktuellen Daten zu aktualisieren oder zu deaktivieren.")

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="zerobot Quantum State Analyse")
    parser.add_argument('--mode',      type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument('--symbol',    type=str, default=None)
    parser.add_argument('--timeframe', type=str, default=None)
    parser.add_argument('--top',       type=int, default=20)
    args = parser.parse_args()

    if args.mode == 4:
        from zerobot.analysis.interactive_chart import run_interactive_chart
        with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
            settings = json.load(f)
        secret_path = os.path.join(PROJECT_ROOT, 'secret.json')
        secrets = {}
        if os.path.exists(secret_path):
            with open(secret_path) as f:
                secrets = json.load(f)
        run_interactive_chart(settings, secrets)
        return

    db = _load_db()
    if db is None:
        sys.exit(1)

    if args.symbol and args.timeframe:
        mode_symbol_detail(db, args.symbol, args.timeframe, args.top)
    elif args.mode == 1:
        mode_overview(db)
    elif args.mode == 2:
        mode_regime_analysis(db)
    elif args.mode == 3:
        mode_decay_status(db)

    db.close()


if __name__ == '__main__':
    main()
