# src/zerobot/utils/timeframe_utils.py
import math


def determine_htf(timeframe):
    """Bestimmt den nächsthöheren Zeitrahmen (mindestens 4x größer) für den MTF-Bias."""
    tf_map = {'5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120, '4h': 240, '6h': 360, '1d': 1440}
    tf_minutes = tf_map.get(timeframe)

    if tf_minutes is None:
        return '4h'

    target_minutes = tf_minutes * 4
    best_htf = timeframe
    min_diff = float('inf')

    for htf_str, htf_min in tf_map.items():
        if htf_min >= target_minutes:
            diff = htf_min - target_minutes
            if diff < min_diff:
                min_diff = diff
                best_htf = htf_str

    if best_htf == timeframe and tf_minutes < 1440:
        return '1d'

    if timeframe == '1d':
        return '1d'

    return best_htf
