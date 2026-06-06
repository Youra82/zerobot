# src/zerobot/strategy/ear_logic.py
import pandas as pd


def get_ear_signal(processed_data: pd.DataFrame, current_candle: pd.Series,
                   params: dict, market_bias=None):
    """
    Liest das vorberechnete EAR-Signal aus dem DataFrame.
    Gibt (side, close_price) zurueck oder (None, None) bei keinem Signal.
    """
    if processed_data is None or processed_data.empty:
        return None, None
    if 'ear_signal' not in current_candle.index:
        return None, None

    signal_val  = current_candle.get('ear_signal', 0)
    close_price = current_candle['close']
    signal_side = None

    if signal_val == 1:
        signal_side = 'buy'
    elif signal_val == -1:
        signal_side = 'sell'

    # MTF-Bias-Filter (optional)
    if signal_side and market_bias and market_bias != 'NEUTRAL':
        if market_bias == 'BULLISH' and signal_side == 'sell':
            return None, None
        if market_bias == 'BEARISH' and signal_side == 'buy':
            return None, None

    if signal_side:
        return signal_side, close_price

    return None, None
