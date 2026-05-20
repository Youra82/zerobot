# src/zerobot/physics/evolver.py
# Physics-Informed Evolution Engine
#
# Bewertet alle Quantum States nach:
#   1. Statistischer Performance (Winrate, Avg Move, Occurrences)
#   2. Physikalischen Qualitaetsmerkmalen:
#      a) Hurst-Alignment: Muster entstand in konsistentem Regime
#      b) Entropy-Bonus: Muster entstand in ruhigem (niedrigem ApEn) Marktumfeld
#   3. Temporaler Decay: aeltere Muster verlieren Gewicht
#
# Erweiterter Score (vs dnabot):
#
#   decay = exp(−age_days / effective_half_life)
#   entropy_bonus  = 1.0 + 0.30 × (1 − mean_apen)     [0.7 – 1.3]
#   hurst_bonus    = 1.0 + 0.20 × |mean_hurst − 0.5|   [1.0 – 1.1]
#   effective_occ  = occ × decay × entropy_bonus × hurst_bonus
#   score = winrate × avg_move_pct × log(1 + effective_occ)
#
# Semantik:
#   - Muster die immer in ruhigen Maerkten auftraten -> zuverlaessiger
#   - Muster mit extremem Hurst -> staerkerer struktureller Edge
#   - Zeitdecay: aeltere Muster die nicht mehr auftreten -> Gewicht sinkt

import math
import json
import logging
from datetime import datetime, timezone

from zerobot.physics.database import StateDB

logger = logging.getLogger(__name__)

SCORED_REGIMES = ['TREND', 'REVERTING', 'NEUTRAL']

_REGIME_COLS = {
    'TREND':     ('occ_trend',   'wins_trend'),
    'REVERTING': ('occ_range',   'wins_range'),
    'NEUTRAL':   ('occ_neutral', 'wins_neutral'),
    'RANGE':     ('occ_range',   'wins_range'),
}


def compute_decay(last_seen_iso: str, effective_half_life: float) -> float:
    """
    Temporaler Decay: exp(−age_days / effective_half_life)

    half_life_days=0 -> kein Decay (nuetzlich fuer Backtests)
    """
    if effective_half_life <= 0:
        return 1.0
    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age_days = max((datetime.now(timezone.utc) - last_seen).days, 0)
        return math.exp(-age_days / effective_half_life)
    except Exception:
        return 1.0


def compute_entropy_bonus(apen_sum: float, apen_count: int) -> float:
    """
    Entropy-Bonus: Pattern die in ruhigen Maerkten (niedriger ApEn) auftreten
    sind zuverlaessiger.

    bonus = 1.0 + 0.30 × (1 − mean_apen)  ∈ [0.70, 1.30]
    """
    if apen_count == 0:
        return 1.0
    mean_apen = min(apen_sum / apen_count, 2.0)  # cap bei 2
    normalized = min(mean_apen, 1.0)              # normalisieren auf [0, 1]
    return 1.0 + 0.30 * (1.0 - normalized)


def compute_hurst_bonus(hurst_sum: float, hurst_count: int) -> float:
    """
    Hurst-Bonus: Pattern mit extremem Hurst haben staerkeren strukturellen Edge.

    bonus = 1.0 + 0.20 × |mean_hurst − 0.5|  ∈ [1.0, 1.10]
    Stark trendend (H=0.7) -> +10% Bonus
    Stark revertierend (H=0.3) -> +10% Bonus
    Neutral (H=0.5) -> kein Bonus
    """
    if hurst_count == 0:
        return 1.0
    mean_hurst = hurst_sum / hurst_count
    deviation = abs(mean_hurst - 0.5)
    return 1.0 + 0.20 * min(deviation, 0.5) / 0.5


def compute_score(
    winrate: float,
    avg_move_pct: float,
    effective_occ: float,
) -> float:
    """
    Finaler Score = Winrate × Avg Move × log(1 + effective_occ)

    effective_occ enthaelt bereits Decay + Entropy-Bonus + Hurst-Bonus.
    """
    if effective_occ < 0.5:
        return 0.0
    return winrate * avg_move_pct * math.log(1.0 + effective_occ)


def evolve(
    db: StateDB,
    market: str = None,
    timeframe: str = None,
    min_samples: int = 5,
    min_winrate: float = 0.45,
    score_threshold: float = 0.08,
    half_life_days: float = 180.0,
    vol_factor: float = 1.0,
) -> dict:
    """
    Bewertet alle Quantum States und aktiviert/deaktiviert sie.

    Args:
        db: StateDB-Instanz
        market: Optional - nur States fuer diesen Markt
        timeframe: Optional - nur States fuer diesen Timeframe
        min_samples: Mindest-Occurrences pro Regime fuer Aktivierung
        min_winrate: Mindest-Winrate
        score_threshold: Mindest-Score nach physics-adjustiertem Decay
        half_life_days: Basis-Halbwertszeit (0 = kein Decay)
        vol_factor: ATR/ATR-MA -> passt Halbwertszeit an Volatilitaet an

    Returns:
        Evolution-Statistiken
    """
    vol_factor_clamped = max(0.5, min(vol_factor, 3.0))
    effective_half_life = half_life_days / vol_factor_clamped if half_life_days > 0 else 0.0

    all_states = db.get_all_states(market=market, timeframe=timeframe)

    activated = 0
    deactivated = 0
    total_regime_activations = {r: 0 for r in SCORED_REGIMES}

    for state in all_states:
        sid = state['state_id']
        avg_move = state['avg_move_pct']

        # Decay
        last_seen = state.get('last_seen') or state.get('last_updated', '')
        decay = compute_decay(last_seen, effective_half_life)

        # Physics-Bonuses
        apen_bonus = compute_entropy_bonus(
            state.get('apen_sum', 0.0) or 0.0,
            state.get('apen_count', 0) or 0
        )
        hurst_bonus = compute_hurst_bonus(
            state.get('hurst_sum', 0.0) or 0.0,
            state.get('hurst_count', 0) or 0
        )

        active_regimes = []
        best_score = 0.0

        for regime in SCORED_REGIMES:
            occ_col, wins_col = _REGIME_COLS[regime]
            occ = state.get(occ_col, 0) or 0
            wins = state.get(wins_col, 0) or 0

            if occ < min_samples:
                continue

            winrate = wins / occ
            # Physics-adjustierte effektive Occurrences
            effective_occ = occ * decay * apen_bonus * hurst_bonus
            score = compute_score(winrate, avg_move, effective_occ)

            if winrate >= min_winrate and score >= score_threshold:
                active_regimes.append(regime)
                best_score = max(best_score, score)
                total_regime_activations[regime] += 1
                logger.debug(
                    f"[{regime}] Aktiv: {state['sequence'][:40]} [{state['direction']}] "
                    f"WR={winrate:.1%} Score={score:.3f} "
                    f"(eff_occ={effective_occ:.1f}, decay={decay:.2f}, "
                    f"apen_bonus={apen_bonus:.2f}, hurst_bonus={hurst_bonus:.2f})"
                )

        # Fallback: globale Stats wenn kein Regime genug Daten hat
        if not active_regimes:
            total = state['total_occurrences'] or 0
            global_wins = state['wins'] or 0
            if total >= min_samples:
                global_winrate = global_wins / total
                effective_occ = total * decay * apen_bonus * hurst_bonus
                best_score = compute_score(global_winrate, avg_move, effective_occ)
                if global_winrate >= min_winrate and best_score >= score_threshold:
                    active_regimes = ['NEUTRAL']
                    total_regime_activations['NEUTRAL'] += 1
            elif total > 0 and best_score == 0.0:
                global_winrate = (state['wins'] or 0) / total
                best_score = compute_score(global_winrate, avg_move, total * decay)

        is_active = len(active_regimes) > 0
        db.update_state_evolution(sid, best_score, is_active, active_regimes)

        if is_active:
            activated += 1
        else:
            deactivated += 1

    logger.info(
        f"[Evolver] {market or 'alle'} ({timeframe or 'alle'}) | "
        f"Gesamt: {len(all_states)} | Aktiv: {activated} | Inaktiv: {deactivated} | "
        f"half_life={half_life_days}d vol_factor={vol_factor_clamped:.2f} "
        f"eff_half_life={effective_half_life:.0f}d"
    )

    return {
        "total": len(all_states),
        "activated": activated,
        "deactivated": deactivated,
        "regime_activations": total_regime_activations,
        "effective_half_life": effective_half_life,
    }


def get_top_states(db: StateDB, market: str, timeframe: str, top_n: int = 20) -> list[dict]:
    states = db.get_active_states_for_market(market, timeframe)
    for s in states:
        s['winrate'] = s['wins'] / max(s['total_occurrences'], 1)
        try:
            s['active_regimes_list'] = json.loads(s.get('active_regimes', '[]'))
        except (json.JSONDecodeError, TypeError):
            s['active_regimes_list'] = []
        # Durchschnittliche Physik-Metriken
        hc = s.get('hurst_count', 0) or 0
        ac = s.get('apen_count', 0) or 0
        s['avg_hurst'] = (s.get('hurst_sum', 0) / hc) if hc > 0 else 0.5
        s['avg_apen'] = (s.get('apen_sum', 0) / ac) if ac > 0 else 1.0
    return sorted(states, key=lambda x: x['score'], reverse=True)[:top_n]


def print_state_report(db: StateDB, market: str = None, timeframe: str = None):
    summary = db.get_db_summary()

    print("\n" + "=" * 72)
    print(f"  QUANTUM STATE LIBRARY - zerobot")
    print("=" * 72)
    print(f"  Gesamt-States: {summary['total_states']}")
    print(f"  Aktive States: {summary['active_states']}")
    print(f"  Maerkte:       {', '.join(summary['markets'])}")
    print("=" * 72)
    print(f"  Top 10 Muster (aktiv, >=5 Samples):")
    print("-" * 72)

    for i, p in enumerate(summary['top_patterns'], 1):
        winrate = p.get('winrate', 0)
        try:
            regimes = json.loads(p.get('active_regimes', '[]'))
        except (json.JSONDecodeError, TypeError):
            regimes = []

        hc = p.get('hurst_count', 0) or 0
        ac = p.get('apen_count', 0) or 0
        avg_h = (p.get('hurst_sum', 0) / hc) if hc > 0 else 0.5
        avg_a = (p.get('apen_sum', 0) / ac) if ac > 0 else 1.0

        print(f"  {i:2}. [{p['direction']:<5}] {p['sequence']}")
        print(
            f"       Score: {p['score']:.3f} | WR: {winrate:.1%} | "
            f"Avg Move: {p.get('avg_move_pct', 0):.2f}% | n={p['total_occurrences']}"
        )
        print(
            f"       Hurst: {avg_h:.3f} | ApEn: {avg_a:.3f} | "
            f"Regime: {', '.join(regimes) if regimes else '-'}"
        )
        print(f"       Markt: {p['market']} | TF: {p['timeframe']}")
        print()

    print("=" * 72)
