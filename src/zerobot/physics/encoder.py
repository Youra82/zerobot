# src/zerobot/physics/encoder.py
# Quantum State Encoder — kodiert Marktkerzen zu physikalischen Zustandsvektoren
#
# State-Format: {DIR}{BODY}{WICK}-{HURST}{ENTROPY}{VOL}
#
# Dimension 1 — DIR (Richtung):
#   B = Bullish (close >= open)
#   S = Bearish (close < open)
#
# Dimension 2 — BODY (Körperstärke relativ zu ATR):
#   1 = klein  (<0.30 ATR)   — Doji / Unsicherheit
#   2 = mittel (0.30–0.80)   — normale Kerze
#   3 = groß   (>0.80 ATR)   — starkes Momentum
#
# Dimension 3 — WICK (Docht-Struktur):
#   U = oberer Docht dominant → Verkaufsdruck / Ablehnung oben
#   D = unterer Docht dominant → Kaufdruck / Ablehnung unten
#   B = beide Dochte prominent → Spinning Top / Unsicherheit
#   N = kein dominanter Docht → Marubozu / klares Momentum
#
# Dimension 4 — HURST (fraktales Marktregime zum Zeitpunkt):
#   T = Trending   (H > 0.55) → Momentum-Muster bevorzugen
#   R = Reverting  (H < 0.45) → Umkehr-Muster bevorzugen
#   N = Neutral    (sonst)    → vorsichtig
#
# Dimension 5 — ENTROPY (Vorhersagbarkeit zum Zeitpunkt):
#   C = Calm    (ApEn niedrig) → Muster wiederholen sich → zuverlässig
#   E = Excited (ApEn hoch)   → chaotisch → Muster unzuverlässig
#
# Dimension 6 — VOL (Volumen relativ zum 20-Perioden-MA):
#   L = unterdurchschnittlich
#   H = überdurchschnittlich
#
# Beispiele:
#   B3N-TCH = Bullish, großer Körper, kein Docht, Trend-Regime, ruhig, hohes Volumen
#             → starker Trend-Einstieg in vorhersagbarem Markt
#   S1B-RCL = Bärisch, kleiner Körper, beide Dochte, Reversion-Regime, ruhig, niedriges Volumen
#             → potenzieller Boden nach Übertreibung

import numpy as np
import pandas as pd
import ta
import logging

from zerobot.physics.hurst import rolling_hurst, classify_hurst
from zerobot.physics.entropy import rolling_apen, classify_entropy

logger = logging.getLogger(__name__)

# Mindestanzahl an Kerzen für ATR, Hurst und ApEn
MIN_CANDLES = 60
HURST_WINDOW = 50
APEN_WINDOW = 20
VOL_MA_PERIOD = 20
ATR_PERIOD = 14


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    return ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'],
        window=period, fillna=True
    ).average_true_range()


def encode_state(
    open_: float, high: float, low: float, close: float, volume: float,
    atr: float, avg_volume: float,
    hurst_regime: str, entropy_regime: str,
) -> str:
    """
    Kodiert eine einzelne Kerze + Marktkontext zu einem Quantum-State-String.

    Args:
        open_, high, low, close, volume: OHLCV-Werte
        atr: Aktueller ATR
        avg_volume: 20-Perioden-Volumen-Durchschnitt
        hurst_regime: 'T', 'R', oder 'N'
        entropy_regime: 'C' oder 'E'

    Returns:
        State-String z.B. "B3N-TCH"
    """
    # ── Richtung ─────────────────────────────────────────────────────────────
    direction = "B" if close >= open_ else "S"

    # ── Körperstärke relativ zu ATR ───────────────────────────────────────────
    body = abs(close - open_)
    if atr > 0:
        body_ratio = body / atr
        if body_ratio < 0.30:
            size = "1"
        elif body_ratio < 0.80:
            size = "2"
        else:
            size = "3"
    else:
        size = "2"

    # ── Docht-Struktur ────────────────────────────────────────────────────────
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    candle_range = high - low
    wick_threshold = body * 0.5 if body > 0 else candle_range * 0.25
    upper_prom = upper_wick > wick_threshold
    lower_prom = lower_wick > wick_threshold

    if upper_prom and lower_prom:
        wick = "B"
    elif upper_prom:
        wick = "U"
    elif lower_prom:
        wick = "D"
    else:
        wick = "N"

    # ── Volumen ───────────────────────────────────────────────────────────────
    vol_rel = "H" if (avg_volume > 0 and volume > avg_volume) else "L"

    return f"{direction}{size}{wick}-{hurst_regime}{entropy_regime}{vol_rel}"


def encode_dataframe(df: pd.DataFrame) -> list[str]:
    """
    Kodiert alle Kerzen eines OHLCV-DataFrames zu Quantum-State-Strings.

    Berechnet:
      - ATR (14-Perioden)
      - Volumen-MA (20-Perioden)
      - Rollierende Hurst-Exponenten (50-Perioden, Single-Scale)
      - Rollierende ApEn (20-Perioden)

    Args:
        df: OHLCV DataFrame (Spalten: open, high, low, close, volume)

    Returns:
        Liste von State-Strings, gleiche Länge wie df.
        Erste Kerzen (< min. Fenster) erhalten neutrale Fallback-States.
    """
    if len(df) < 10:
        return []

    closes = df['close'].values.astype(float)
    volumes = df['volume'].values.astype(float)

    # ATR
    atr_series = compute_atr(df).values

    # Volumen-MA
    vol_ma = pd.Series(volumes).rolling(window=VOL_MA_PERIOD, min_periods=1).mean().values

    # Hurst (rolling, Single-Scale für Performance)
    hurst_values = rolling_hurst(closes, window=HURST_WINDOW, multiscale=False)

    # ApEn (rolling)
    apen_values = rolling_apen(closes, window=APEN_WINDOW)

    states = []
    for i in range(len(df)):
        row = df.iloc[i]
        hurst_code = classify_hurst(hurst_values[i])
        entropy_code = classify_entropy(apen_values[i])

        state = encode_state(
            open_=float(row['open']),
            high=float(row['high']),
            low=float(row['low']),
            close=float(row['close']),
            volume=float(row['volume']),
            atr=float(atr_series[i]),
            avg_volume=float(vol_ma[i]),
            hurst_regime=hurst_code,
            entropy_regime=entropy_code,
        )
        states.append(state)

    return states


def decode_state(state: str) -> dict:
    """Dekodiert einen State-String zu lesbaren Merkmalen."""
    if len(state) < 7:
        return {"raw": state}

    parts = state.split("-")
    if len(parts) != 2 or len(parts[0]) != 3 or len(parts[1]) != 3:
        return {"raw": state}

    main, ext = parts[0], parts[1]

    return {
        "state": state,
        "direction":  {"B": "Bullish", "S": "Bearish"}.get(main[0], main[0]),
        "body_size":  {"1": "klein (<30% ATR)", "2": "mittel", "3": "groß (>80% ATR)"}.get(main[1], main[1]),
        "wick":       {"U": "oben dominant", "D": "unten dominant", "B": "beide", "N": "keiner"}.get(main[2], main[2]),
        "hurst":      {"T": "Trend (H>0.55)", "R": "Reversion (H<0.45)", "N": "Neutral"}.get(ext[0], ext[0]),
        "entropy":    {"C": "ruhig (ApEn niedrig)", "E": "aufgeregt (ApEn hoch)"}.get(ext[1], ext[1]),
        "volume":     {"L": "niedrig", "H": "hoch"}.get(ext[2], ext[2]),
    }


def states_to_sequence_string(states: list[str]) -> str:
    return "|".join(states)


def sequence_string_to_states(sequence: str) -> list[str]:
    return sequence.split("|")


def get_current_physics_metrics(df: pd.DataFrame) -> dict:
    """
    Berechnet aktuelle Physik-Metriken für die letzten Kerzen.
    Wird im Signal-Generator genutzt für Confidence-Berechnung.

    Returns:
        dict mit hurst, hurst_regime, apen, entropy_regime
    """
    closes = df['close'].values.astype(float)

    hurst_vals = rolling_hurst(closes, window=min(HURST_WINDOW, len(closes)))
    apen_vals = rolling_apen(closes, window=min(APEN_WINDOW, len(closes)))

    current_hurst = float(hurst_vals[-1])
    current_apen = float(apen_vals[-1])

    return {
        "hurst": current_hurst,
        "hurst_regime": classify_hurst(current_hurst),
        "apen": current_apen,
        "entropy_regime": classify_entropy(current_apen),
    }
