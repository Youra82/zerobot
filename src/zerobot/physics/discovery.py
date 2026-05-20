# src/zerobot/physics/discovery.py
# Quantum Pattern Discovery Engine
#
# Ablauf:
#   1. OHLCV-Daten laden und Kerzen zu Quantum States kodieren
#   2. Physik-Metriken (Hurst, ApEn) für jeden Zeitpunkt berechnen
#   3. Sliding-Window über alle States (Längen 3, 4, 5)
#   4. Für jedes Fenster: Outcome beobachten (5 Kerzen Horizont)
#   5. LONG: max Up > Threshold UND > max Down
#   6. SHORT: max Down > Threshold UND > max Up
#   7. State-DB aktualisieren mit Hurst + ApEn am Zeitpunkt
#
# Unterschied zu dnabot:
#   - Kürzere Sequenzen (3-5 statt 4-6): reicherer State = mehr Diskriminationskraft
#   - Hurst + ApEn werden pro Muster gespeichert → Evolver kann physics-alignment prüfen
#   - Regime-Erkennung nutzt Hurst statt nur ADX

import logging
import numpy as np
import pandas as pd

from zerobot.physics.encoder import encode_dataframe, states_to_sequence_string
from zerobot.physics.database import StateDB
from zerobot.physics.hurst import rolling_hurst, classify_hurst
from zerobot.physics.entropy import rolling_apen

logger = logging.getLogger(__name__)

REGIME_RECALC_INTERVAL = 20


def _hurst_to_regime(h: float) -> str:
    """Mappt Hurst-Exponent auf Regime-String (kompatibel mit DB-Schema)."""
    code = classify_hurst(h)
    return {'T': 'TREND', 'R': 'REVERTING', 'N': 'NEUTRAL'}[code]


def discover_states(
    df: pd.DataFrame,
    market: str,
    timeframe: str,
    db: StateDB,
    sequence_lengths: list[int] = None,
    discovery_horizon: int = 5,
    move_threshold_pct: float = 1.0,
) -> dict:
    """
    Scannt historische OHLCV-Daten und entdeckt profitable Quantum-State-Muster.

    Args:
        df: OHLCV DataFrame (index=Timestamp)
        market: Handelspaar z.B. "BTC/USDT:USDT"
        timeframe: Zeitrahmen z.B. "4h"
        db: StateDB-Instanz
        sequence_lengths: Fenstergrößen (Standard: [3, 4, 5])
        discovery_horizon: Kerzen nach der Sequenz beobachten
        move_threshold_pct: Mindestbewegung in % für gültiges Outcome

    Returns:
        Statistik-Dict
    """
    if sequence_lengths is None:
        sequence_lengths = [3, 4, 5]

    if len(df) < 60:
        logger.warning(f"Zu wenig Daten: {market} ({timeframe}): {len(df)} Kerzen.")
        return {"candles_processed": 0, "new_states": 0, "updated_states": 0}

    logger.info(
        f"[Discovery] {market} ({timeframe}) | {len(df)} Kerzen | "
        f"Horizon={discovery_horizon} | Threshold={move_threshold_pct}%"
    )

    # Alle States berechnen (mit Hurst + ApEn eingebettet im State-Code)
    states = encode_dataframe(df)
    closes = df['close'].values.astype(float)
    highs = df['high'].values.astype(float)
    lows = df['low'].values.astype(float)

    # Rohe Physik-Metriken für DB-Speicherung
    hurst_values = rolling_hurst(closes, window=50, multiscale=False)
    apen_values = rolling_apen(closes, window=20)

    new_states = 0
    updated_states = 0
    threshold_factor = move_threshold_pct / 100.0

    # Hurst-Regime-Cache
    regime_cache: dict[int, str] = {}

    def get_regime_at(idx: int) -> str:
        bucket = (idx // REGIME_RECALC_INTERVAL) * REGIME_RECALC_INTERVAL
        if bucket not in regime_cache:
            h = float(hurst_values[min(bucket, len(hurst_values) - 1)])
            regime_cache[bucket] = _hurst_to_regime(h)
        return regime_cache[bucket]

    for seq_len in sequence_lengths:
        max_start = len(states) - seq_len - discovery_horizon
        if max_start <= 0:
            continue

        logger.debug(f"  seq_len={seq_len} | {max_start} Fenster...")

        for i in range(max_start):
            seq_states = states[i:i + seq_len]
            sequence = states_to_sequence_string(seq_states)

            entry_idx = i + seq_len
            entry_price = closes[entry_idx - 1]

            if entry_price <= 0:
                continue

            # Regime zum Zeitpunkt der Sequenz
            regime = get_regime_at(i)

            # Physik-Metriken am Ende der Sequenz
            hurst_at_entry = float(hurst_values[entry_idx - 1])
            apen_at_entry = float(apen_values[entry_idx - 1])

            # Zukunft beobachten (strikt nach Sequenz-Close)
            future_highs = highs[entry_idx: entry_idx + discovery_horizon]
            future_lows = lows[entry_idx: entry_idx + discovery_horizon]

            if len(future_highs) == 0:
                continue

            max_high = float(future_highs.max())
            min_low = float(future_lows.min())

            max_up_pct = (max_high - entry_price) / entry_price
            max_down_pct = (entry_price - min_low) / entry_price

            long_outcome = (max_up_pct >= threshold_factor) and (max_up_pct > max_down_pct)
            short_outcome = (max_down_pct >= threshold_factor) and (max_down_pct > max_up_pct)

            for direction, is_win, move in [
                ("LONG",  long_outcome,  max_up_pct   * 100.0),
                ("SHORT", short_outcome, max_down_pct * 100.0),
            ]:
                is_new = db.upsert_state_outcome(
                    sequence=sequence,
                    market=market,
                    timeframe=timeframe,
                    direction=direction,
                    seq_length=seq_len,
                    is_win=is_win,
                    move_pct=move,
                    regime=regime,
                    hurst_value=hurst_at_entry,
                    apen_value=apen_at_entry,
                )
                if is_new:
                    new_states += 1
                else:
                    updated_states += 1

    candles_processed = len(df)
    db.log_scan(market, timeframe, candles_processed, new_states, updated_states)

    logger.info(
        f"[Discovery] {market} ({timeframe}) fertig: "
        f"{candles_processed} Kerzen, {new_states} neue States, {updated_states} aktualisiert."
    )

    return {
        "candles_processed": candles_processed,
        "new_states": new_states,
        "updated_states": updated_states,
    }
