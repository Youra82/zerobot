# src/zerobot/physics/hurst.py
# Hurst-Exponent Berechnung — misst fraktale Marktstruktur
#
# H > 0.55 → TRENDING  (persistente Bewegung — Trend folgen)
# H < 0.45 → REVERTING (antipersistent — gegen den Trend)
# H ≈ 0.50 → NEUTRAL   (Zufallspfad — kein vorhersagbarer Vorteil)
#
# Methoden:
#   R/S-Analyse (Hurst 1951) — klassisch, robust
#   Multi-Scale R/S — genauer für kurze Fenster
#
# Verwendung im zerobot:
#   - Regime-Erkennung (ersetzt/ergänzt ADX)
#   - State-Encoding: Jede Kerze erhält Hurst-Regime-Code (T/R/N)
#   - Evolver-Gewichtung: Pattern in konsistentem Regime > Pattern in gemischtem

import numpy as np
import logging

logger = logging.getLogger(__name__)


def hurst_rs_single(ts: np.ndarray) -> float:
    """
    Berechnet den Hurst-Exponenten mit der R/S-Methode für ein einzelnes Fenster.

    Schnell — für Rolling-Berechnung pro Kerze geeignet.

    Formel:
        R/S = (max(X) - min(X)) / std(X)  wobei X = kumulierte Abweichungen vom Mittel
        H ≈ log(R/S) / log(n)

    Args:
        ts: Zeitreihe (Preise oder Returns), mind. 10 Werte

    Returns:
        Hurst-Exponent zwischen 0.0 und 1.0, Fallback 0.5
    """
    ts = np.array(ts, dtype=float)
    n = len(ts)
    if n < 10:
        return 0.5

    mean = np.mean(ts)
    deviations = ts - mean
    Z = np.cumsum(deviations)
    R = np.max(Z) - np.min(Z)
    S = np.std(ts, ddof=1)

    if S < 1e-12 or R < 1e-12:
        return 0.5

    h = np.log(R / S) / np.log(n)
    return float(np.clip(h, 0.01, 0.99))


def hurst_rs_multiscale(ts: np.ndarray, min_window: int = 8, n_scales: int = 6) -> float:
    """
    Multi-Scale R/S Hurst — genauer als Single-Window.
    Passt eine Regression über log(R/S) vs log(lag) an.

    Geeignet für Offline-Discovery (scan_and_learn.py) — etwas langsamer.

    Args:
        ts: Zeitreihe (Preise), mind. 50 Werte für verlässliche Ergebnisse
        min_window: Kleinstes Fenster für R/S
        n_scales: Anzahl der Log-Skalen

    Returns:
        Hurst-Exponent zwischen 0.0 und 1.0
    """
    ts = np.array(ts, dtype=float)
    n = len(ts)
    if n < min_window * 2:
        return hurst_rs_single(ts)

    # Log-gleichmäßig verteilte Fenstergrößen
    max_window = n // 2
    lags = np.unique(
        np.logspace(np.log10(min_window), np.log10(max_window), n_scales).astype(int)
    )
    lags = lags[lags >= min_window]

    rs_values = []
    lag_values = []

    for lag in lags:
        n_chunks = n // lag
        if n_chunks < 2:
            continue

        rs_chunk = []
        for j in range(n_chunks):
            chunk = ts[j * lag:(j + 1) * lag]
            mean_c = np.mean(chunk)
            dev = chunk - mean_c
            Z = np.cumsum(dev)
            R = np.max(Z) - np.min(Z)
            S = np.std(chunk, ddof=1)
            if S > 1e-12 and R > 1e-12:
                rs_chunk.append(R / S)

        if rs_chunk:
            rs_values.append(np.log(np.mean(rs_chunk)))
            lag_values.append(np.log(lag))

    if len(rs_values) < 3:
        return hurst_rs_single(ts)

    # Lineare Regression: log(R/S) = H * log(lag) + const
    try:
        coeffs = np.polyfit(lag_values, rs_values, 1)
        h = float(np.clip(coeffs[0], 0.01, 0.99))
        return h
    except Exception:
        return hurst_rs_single(ts)


def rolling_hurst(
    prices: np.ndarray,
    window: int = 50,
    multiscale: bool = False,
) -> np.ndarray:
    """
    Berechnet den Hurst-Exponenten über ein rollendes Fenster.

    Args:
        prices: Preisreihe (close-Preise)
        window: Fenstergröße (mind. 20, empfohlen 50)
        multiscale: True = genauer aber langsamer (für Discovery)

    Returns:
        Array gleicher Länge wie prices, Werte [0, 1], Anfang = 0.5 (Fallback)
    """
    n = len(prices)
    result = np.full(n, 0.5)

    func = hurst_rs_multiscale if multiscale else hurst_rs_single

    for i in range(window, n + 1):
        chunk = prices[i - window:i]
        result[i - 1] = func(chunk)

    return result


def classify_hurst(h: float, trend_threshold: float = 0.55, revert_threshold: float = 0.45) -> str:
    """
    Klassifiziert einen Hurst-Wert in ein Regime.

    Returns:
        'T' (Trend, H > 0.55)
        'R' (Reversion, H < 0.45)
        'N' (Neutral, dazwischen)
    """
    if h > trend_threshold:
        return 'T'
    elif h < revert_threshold:
        return 'R'
    return 'N'


def get_current_hurst(prices: np.ndarray, window: int = 50) -> tuple[float, str]:
    """
    Berechnet aktuellen Hurst + Regime für die letzten `window` Preise.

    Returns:
        (hurst_value, regime_code) z.B. (0.62, 'T')
    """
    if len(prices) < window:
        return 0.5, 'N'
    chunk = prices[-window:]
    h = hurst_rs_single(chunk)
    regime = classify_hurst(h)
    return h, regime
