# src/zerobot/strategy/signal_logic.py
# Signal-Generator: Quantum State + Transfer Entropy + Multi-Gate Konfidenz
#
# 4 Stufen bis zum Signal:
#   1. Quantum State Match: letzte Kerzen als State-Sequenz → DB-Abfrage
#   2. Regime-Filter: Hurst-basiertes Regime stimmt mit State überein
#   3. Entropy-Filter: aktueller Markt nicht zu chaotisch (ApEn)
#   4. TE-Boost: wenn BTC-Daten verfügbar → Transfer Entropy → Score-Multiplikator
#
# Nur wenn alle 4 Gates passiert → Trade
#
# Signal-Format (kompatibel mit trade_manager.py):
#   {
#     "side":             "long" | "short",
#     "entry_price":      float,
#     "sl_price":         float,
#     "tp_price":         float,
#     "sl_pct":           float,
#     "state_id":         str,
#     "sequence":         str,
#     "score":            float,
#     "raw_score":        float,   ← vor TE-Boost
#     "te_boost":         float,
#     "winrate":          float,
#     "total_occurrences": int,
#     "seq_length":       int,
#     "avg_move_pct":     float,
#     "regime":           str,
#     "hurst":            float,
#     "apen":             float,
#     "confidence":       float,   ← kombiniertes Konfidenz-Score [0-1]
#   }

import json
import logging
import pandas as pd
import numpy as np
from typing import Optional

from zerobot.physics.encoder import encode_dataframe, states_to_sequence_string, get_current_physics_metrics
from zerobot.physics.database import StateDB
from zerobot.physics.entropy import compute_te_boost
from zerobot.physics.hurst import classify_hurst

logger = logging.getLogger(__name__)

MIN_CANDLES_REQUIRED = 60


def _hurst_to_db_regime(hurst_code: str) -> str:
    return {'T': 'TREND', 'R': 'REVERTING', 'N': 'NEUTRAL'}[hurst_code]


def _build_signal(
    side: str,
    df: pd.DataFrame,
    state: dict,
    physics: dict,
    te_boost: float,
    rr_ratio: float = 2.0,
) -> dict:
    """Baut das finale Signal-Dict."""
    seq_len = state['seq_length']
    seq_candles = df.iloc[-seq_len:]
    last_close = float(df['close'].iloc[-1])
    winrate = state['wins'] / max(state['total_occurrences'], 1)

    if side == 'long':
        sl_price = float(seq_candles['low'].min())
        sl_distance = last_close - sl_price
        if sl_distance <= 0:
            sl_price = last_close * 0.98
            sl_distance = last_close - sl_price
        tp_price = last_close + (rr_ratio * sl_distance)
    else:
        sl_price = float(seq_candles['high'].max())
        sl_distance = sl_price - last_close
        if sl_distance <= 0:
            sl_price = last_close * 1.02
            sl_distance = sl_price - last_close
        tp_price = last_close - (rr_ratio * sl_distance)

    sl_pct = (sl_distance / last_close) * 100.0
    raw_score = state['score']
    boosted_score = raw_score * te_boost

    # Konfidenz: kombiniert Winrate, Score-Stärke, Physics-Alignment [0-1]
    wr_norm = min(max((winrate - 0.45) / 0.35, 0.0), 1.0)
    score_norm = min(boosted_score / 2.0, 1.0)
    h = physics.get('hurst', 0.5)
    hurst_strength = min(abs(h - 0.5) / 0.3, 1.0)
    confidence = 0.50 * wr_norm + 0.30 * score_norm + 0.20 * hurst_strength

    logger.info(
        f"[Signal] {side.upper()} | Entry: {last_close:.4f} | "
        f"SL: {sl_price:.4f} ({sl_pct:.2f}%) | TP: {tp_price:.4f} | "
        f"Score: {boosted_score:.3f} (raw: {raw_score:.3f} × TE: {te_boost:.2f}) | "
        f"WR: {winrate:.1%} | Konfidenz: {confidence:.2f} | "
        f"Hurst: {h:.3f} | ApEn: {physics.get('apen', 1.0):.3f}"
    )

    return {
        "side":              side,
        "entry_price":       last_close,
        "sl_price":          sl_price,
        "sl_pct":            sl_pct,
        "tp_price":          tp_price,
        "state_id":          state['state_id'],
        "sequence":          state['sequence'],
        "score":             boosted_score,
        "raw_score":         raw_score,
        "te_boost":          te_boost,
        "winrate":           winrate,
        "total_occurrences": state['total_occurrences'],
        "seq_length":        seq_len,
        "avg_move_pct":      state['avg_move_pct'],
        "regime":            physics.get('hurst_regime', 'N'),
        "hurst":             h,
        "apen":              physics.get('apen', 1.0),
        "confidence":        round(confidence, 3),
    }


def _regime_active(state: dict, current_hurst_code: str) -> bool:
    """
    Prüft ob ein State im aktuellen Hurst-Regime gehandelt werden darf.
    """
    current_db_regime = _hurst_to_db_regime(current_hurst_code)
    if current_db_regime == 'REVERTING':
        current_db_regime = 'REVERTING'

    try:
        active_regimes = json.loads(state.get('active_regimes', '[]'))
    except (json.JSONDecodeError, TypeError):
        active_regimes = []

    if not active_regimes:
        return True

    # TREND-States auch im NEUTRAL-Regime erlaubt (Übergangsphase)
    if current_db_regime == 'NEUTRAL':
        return any(r in active_regimes for r in ['TREND', 'NEUTRAL'])

    return current_db_regime in active_regimes


def get_quantum_signal(
    df: pd.DataFrame,
    params: dict,
    db: StateDB,
    btc_df: Optional[pd.DataFrame] = None,
) -> Optional[dict]:
    """
    Analysiert die letzten Kerzen und gibt das beste Quantum-State-Signal zurück.

    Args:
        df: OHLCV DataFrame des Zielsymbols
        params: Config (market, timeframe, physics, risk, behavior)
        db: StateDB-Instanz
        btc_df: BTC OHLCV DataFrame (für Transfer Entropy, optional)

    Returns:
        Signal-Dict oder None (kein qualifiziertes Signal)
    """
    if len(df) < MIN_CANDLES_REQUIRED:
        logger.warning(f"Zu wenig Kerzen ({len(df)}). Minimum: {MIN_CANDLES_REQUIRED}")
        return None

    market = params['market']['symbol']
    timeframe = params['market']['timeframe']
    physics_cfg = params.get('physics', {})
    min_score = params.get('physics', {}).get('min_score', 0.08)
    rr_ratio = params.get('risk', {}).get('rr_ratio', 2.0)
    sequence_lengths = physics_cfg.get('sequence_lengths', [3, 4, 5])
    max_apen = physics_cfg.get('max_apen_for_trade', 1.5)

    # ── Gate 1: Quantum States kodieren ──────────────────────────────────────
    states = encode_dataframe(df)
    if len(states) < max(sequence_lengths):
        return None

    # ── Gate 2: Physik-Metriken ───────────────────────────────────────────────
    physics = get_current_physics_metrics(df)
    current_hurst = physics['hurst']
    current_hurst_code = physics['hurst_regime']
    current_apen = physics['apen']

    logger.info(
        f"[Physics] Hurst={current_hurst:.3f} ({current_hurst_code}) | "
        f"ApEn={current_apen:.3f}"
    )

    # ── Gate 3: Entropy-Filter ────────────────────────────────────────────────
    if current_apen > max_apen:
        logger.info(f"[Entropy Filter] ApEn={current_apen:.3f} > {max_apen} → Markt zu chaotisch. Kein Signal.")
        return None

    # ── Gate 4: Transfer Entropy Boost ───────────────────────────────────────
    te_boost = 1.0
    if btc_df is not None and physics_cfg.get('transfer_entropy_enabled', True):
        target_closes = df['close'].values
        btc_closes = btc_df['close'].values
        te_threshold = physics_cfg.get('te_threshold', 0.02)
        boost_factor = physics_cfg.get('te_boost_factor', 1.25)

        te_boost = compute_te_boost(
            btc_prices=btc_closes,
            target_prices=target_closes,
            te_threshold=te_threshold,
            boost_factor=boost_factor,
            window=100,
        )

    # ── Signalsuche ───────────────────────────────────────────────────────────
    best_signal = None
    best_score = -1.0

    # Längste Sequenz zuerst (spezifischer = besser)
    for seq_len in sorted(sequence_lengths, reverse=True):
        if len(states) < seq_len:
            continue

        sequence = states_to_sequence_string(states[-seq_len:])

        for direction, side in [("LONG", "long"), ("SHORT", "short")]:
            if direction == "LONG" and not params.get('behavior', {}).get('use_longs', True):
                continue
            if direction == "SHORT" and not params.get('behavior', {}).get('use_shorts', True):
                continue

            state = db.get_state(sequence, market, timeframe, direction)
            if not state:
                continue
            if not state['active']:
                continue
            if state['score'] < min_score:
                continue
            if not _regime_active(state, current_hurst_code):
                continue

            boosted_score = state['score'] * te_boost
            if boosted_score > best_score:
                best_score = boosted_score
                best_signal = _build_signal(side, df, state, physics, te_boost, rr_ratio)

    if best_signal:
        logger.info(
            f"[Quantum Signal] {best_signal['side'].upper()} | "
            f"Score: {best_signal['score']:.3f} | "
            f"Konfidenz: {best_signal['confidence']:.2f}"
        )
    else:
        logger.info(f"[Quantum Signal] Kein Match für {market} ({timeframe}).")

    return best_signal


def update_state_with_trade_result(
    db: StateDB,
    state_id: str,
    sequence: str,
    market: str,
    timeframe: str,
    direction: str,
    seq_length: int,
    outcome: str,
    actual_move_pct: float,
    regime: str = 'NEUTRAL',
    hurst_value: float = 0.5,
    apen_value: float = 1.0,
):
    """Self-Learning: Aktualisiert die DB nach einem Live-Trade."""
    is_win = outcome == "WIN"
    db.upsert_state_outcome(
        sequence=sequence,
        market=market,
        timeframe=timeframe,
        direction=direction,
        seq_length=seq_length,
        is_win=is_win,
        move_pct=abs(actual_move_pct),
        regime=regime,
        hurst_value=hurst_value,
        apen_value=apen_value,
    )
    logger.info(
        f"[Self-Learning] State {state_id[:8]}... aktualisiert: "
        f"outcome={outcome}, move={actual_move_pct:.2f}%, regime={regime}, "
        f"hurst={hurst_value:.3f}, apen={apen_value:.3f}"
    )
