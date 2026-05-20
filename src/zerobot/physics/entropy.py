# src/zerobot/physics/entropy.py
# Informationstheoretische Messgrößen für Märkte
#
# Shannon-Entropie H: misst Unordnung/Unvorhersagbarkeit der Returns
#   H → 0: Markt sehr vorhersagbar (seltene Chance!)
#   H → max: Markt vollständig zufällig (kein Edge)
#
# Approximate Entropy (ApEn): misst Pattern-Komplexität der Preisserie
#   ApEn → 0: Stark repetitiv → Pattern gelten länger
#   ApEn → 2: Kein wiederkehrendes Muster
#
# Transfer Entropy (TE): misst gerichteten Informationsfluss X → Y
#   TE(BTC→ALT) > 0: BTC führt ALT mit zeitlichem Vorlauf
#   Nutzung: Score-Boost wenn BTC-Signal und TE groß

import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Shannon Entropy ──────────────────────────────────────────────────────────

def shannon_entropy(returns: np.ndarray, bins: int = 10) -> float:
    """
    Shannon-Entropie H der Return-Verteilung.

    H = -Σ p_i × log2(p_i)

    Args:
        returns: 1D Return-Array (log returns oder pct_change)
        bins: Anzahl der Bins für die Diskretisierung

    Returns:
        Entropie in Bit [0, log2(bins)]. Normalisiert auf [0, 1].
    """
    if len(returns) < 5:
        return 1.0

    counts, _ = np.histogram(returns, bins=bins)
    probs = counts / counts.sum()
    probs = probs[probs > 0]

    h_raw = -np.sum(probs * np.log2(probs))
    h_max = np.log2(bins)

    return float(h_raw / h_max) if h_max > 0 else 1.0


def rolling_shannon_entropy(prices: np.ndarray, window: int = 20, bins: int = 8) -> np.ndarray:
    """
    Rollierende Shannon-Entropie über ein gleitendes Fenster.

    Returns:
        Array gleicher Länge wie prices, Anfang = 1.0 (max Entropie als Fallback)
    """
    n = len(prices)
    result = np.full(n, 1.0)

    if n < 3:
        return result

    log_returns = np.diff(np.log(np.maximum(prices, 1e-10)))

    for i in range(window, n):
        chunk = log_returns[max(0, i - window):i]
        result[i] = shannon_entropy(chunk, bins=bins)

    return result


# ─── Approximate Entropy (ApEn) ──────────────────────────────────────────────

def approximate_entropy(ts: np.ndarray, m: int = 2, r_factor: float = 0.2) -> float:
    """
    Approximate Entropy — misst die Komplexität/Regularität einer Zeitreihe.

    ApEn(m, r, N) = Φ_m(r) - Φ_{m+1}(r)
    Φ_m(r) = (1/(N-m+1)) × Σ ln(C_i^m(r) / (N-m+1))

    Niedrig (nahe 0) → regelmäßig, vorhersagbar
    Hoch (nahe 2)   → irregular, komplex

    Args:
        ts: Zeitreihe (z.B. log returns)
        m: Template-Länge (typisch 2)
        r_factor: Toleranz als Anteil der Standardabweichung (typisch 0.15-0.25)

    Returns:
        ApEn-Wert >= 0
    """
    ts = np.array(ts, dtype=float)
    N = len(ts)
    if N < m + 3:
        return 0.0

    std = np.std(ts)
    if std < 1e-12:
        return 0.0

    r = r_factor * std

    def phi(m_):
        templates = np.array([ts[i:i + m_] for i in range(N - m_ + 1)])
        count = np.sum(
            np.max(np.abs(templates[:, None] - templates[None, :]), axis=2) <= r,
            axis=1
        )
        count = np.maximum(count, 1)
        return np.mean(np.log(count / (N - m_ + 1)))

    try:
        result = phi(m) - phi(m + 1)
        return max(0.0, float(result))
    except Exception as e:
        logger.debug(f"ApEn Fehler: {e}")
        return 0.0


def rolling_apen(prices: np.ndarray, window: int = 20, m: int = 2, r_factor: float = 0.2) -> np.ndarray:
    """
    Rollierendes Approximate Entropy.

    Returns:
        Array gleicher Länge. Werte nahe 0 = Markt ist vorhersagbar.
    """
    n = len(prices)
    result = np.full(n, 1.0)

    if n < 3:
        return result

    log_returns = np.diff(np.log(np.maximum(prices, 1e-10)))

    for i in range(window, n):
        chunk = log_returns[max(0, i - window):i]
        result[i] = approximate_entropy(chunk, m=m, r_factor=r_factor)

    return result


def classify_entropy(apen: float, threshold: float = 0.5) -> str:
    """
    Klassifiziert ApEn in: 'C' (calm/ruhig) oder 'E' (excited/aufgeregt).

    'C' = niedriger ApEn → Muster wiederholen sich → Pattern zuverlässiger
    'E' = hoher ApEn → chaotisch → Pattern unzuverlässiger
    """
    return 'C' if apen < threshold else 'E'


# ─── Transfer Entropy ─────────────────────────────────────────────────────────

def transfer_entropy(
    x_prices: np.ndarray,
    y_prices: np.ndarray,
    lag: int = 1,
    bins: int = 3,
    window: Optional[int] = None,
) -> float:
    """
    Transfer Entropy TE(X→Y): gerichteter Informationsfluss von X nach Y.

    TE(X→Y) = H(Y_t+1 | Y_t) − H(Y_t+1 | Y_t, X_t)

    Positiv → X (z.B. BTC) enthält Information über zukünftiges Y (ALT)
    ~0      → kein Vorlauf von X auf Y
    Negativ → Rauschen (ignorieren)

    Implementierung:
      - Diskretisierung der Log-Returns in `bins` Zustandsklassen
      - Konditionale Entropien via Häufigkeitszählung
      - Kein ML benötigt — rein informationstheoretisch

    Args:
        x_prices: Preise der führenden Serie (z.B. BTC)
        y_prices: Preise der folgenden Serie (z.B. ETH)
        lag: Zeitversatz in Kerzen (1 = nächste Kerze)
        bins: Anzahl der Zustandsklassen (3 = runter/flach/rauf)
        window: Wenn gesetzt: nur letzte `window` Punkte verwenden

    Returns:
        TE-Wert in Nats. Typisch 0.0–0.3 für reale Marktdaten.
    """
    x_prices = np.array(x_prices, dtype=float)
    y_prices = np.array(y_prices, dtype=float)

    # Auf gemeinsame Länge kürzen
    min_len = min(len(x_prices), len(y_prices))
    if min_len < 20:
        return 0.0

    x_prices = x_prices[-min_len:]
    y_prices = y_prices[-min_len:]

    if window is not None:
        x_prices = x_prices[-window:]
        y_prices = y_prices[-window:]

    # Log-Returns berechnen
    x_ret = np.diff(np.log(np.maximum(x_prices, 1e-10)))
    y_ret = np.diff(np.log(np.maximum(y_prices, 1e-10)))

    n = min(len(x_ret), len(y_ret)) - lag
    if n < 15:
        return 0.0

    x_ret = x_ret[:n + lag]
    y_ret = y_ret[:n + lag]

    # Diskretisierung: Quantile-Binning (robust gegen Ausreißer)
    x_q = np.percentile(x_ret, np.linspace(0, 100, bins + 1)[1:-1])
    y_q = np.percentile(y_ret, np.linspace(0, 100, bins + 1)[1:-1])

    x_disc = np.digitize(x_ret, x_q)
    y_disc = np.digitize(y_ret, y_q)

    y_past   = y_disc[:n]
    y_future = y_disc[lag:n + lag]
    x_past   = x_disc[:n]

    def conditional_entropy(target: np.ndarray, condition: np.ndarray) -> float:
        unique_cond = np.unique(condition)
        h = 0.0
        for c in unique_cond:
            mask = condition == c
            p_c = np.mean(mask)
            if p_c < 1e-12:
                continue
            sub = target[mask]
            vals, counts = np.unique(sub, return_counts=True)
            probs = counts / counts.sum()
            h_sub = -np.sum(probs * np.log(probs + 1e-12))
            h += p_c * h_sub
        return h

    h_y_given_ypast = conditional_entropy(y_future, y_past)

    # Kombinierten Zustand bilden: y_past × bins + x_past
    combined = y_past * bins + x_past
    h_y_given_both = conditional_entropy(y_future, combined)

    te = h_y_given_ypast - h_y_given_both
    return max(0.0, float(te))


def compute_te_boost(
    btc_prices: Optional[np.ndarray],
    target_prices: np.ndarray,
    te_threshold: float = 0.02,
    boost_factor: float = 1.25,
    window: int = 100,
) -> float:
    """
    Berechnet den TE-Boost-Faktor für ein Signal.

    Wenn BTC → Target TE signifikant ist, bekommt das Signal einen Score-Boost.

    Args:
        btc_prices: BTC-Preisreihe (None wenn kein BTC-Referenzdaten)
        target_prices: Ziel-Preisreihe
        te_threshold: Mindest-TE für Boost-Aktivierung
        boost_factor: Score-Multiplikator (z.B. 1.25 = +25%)
        window: Fenster für TE-Berechnung

    Returns:
        Boost-Faktor: 1.0 (kein Boost) oder boost_factor (Boost aktiv)
    """
    if btc_prices is None or len(btc_prices) < 20:
        return 1.0

    try:
        te = transfer_entropy(btc_prices, target_prices, lag=1, bins=3, window=window)
        if te >= te_threshold:
            logger.debug(f"TE(BTC→target)={te:.4f} ≥ {te_threshold} → Boost x{boost_factor}")
            return boost_factor
        logger.debug(f"TE(BTC→target)={te:.4f} < {te_threshold} → kein Boost")
    except Exception as e:
        logger.debug(f"TE-Berechnung fehlgeschlagen: {e}")

    return 1.0
