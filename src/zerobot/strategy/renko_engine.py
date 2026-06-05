# src/zerobot/strategy/renko_engine.py
# RENKO 2-BRICK REVERSAL Signal-Generator
#
# Strategie-Kern:
#   1. Renko-Bricks aus OHLCV berechnen (Brick-Size = ATR * atr_multiplier)
#   2. Trend erkennen: >= trend_min_bricks konsekutive Bricks in eine Richtung
#   3. Signal: Wenn reversal_bricks Bricks in Gegenrichtung folgen
#      LONG:  >= trend_min_bricks DOWN-Bricks, dann reversal_bricks UP-Bricks
#      SHORT: >= trend_min_bricks UP-Bricks,   dann reversal_bricks DOWN-Bricks
#   4. Optionaler Volumen-Filter: Signal nur wenn Volumen > vol_ratio x Durchschnitt
#
# Vorteile gegenüber SR-Zonen:
#   - Kein Noise (Zeit spielt keine Rolle, nur Preis-Bewegung)
#   - Klare mechanische Regeln
#   - Kein DB-Training erforderlich

import pandas as pd
import numpy as np
import warnings

warnings.simplefilter(action='ignore', category=FutureWarning)


class RenkoEngine:
    """
    Konvertiert OHLCV-Kerzen zu Renko-Bricks und erkennt 2-Brick-Reversal-Signale.
    """

    def __init__(self, settings: dict):
        self.atr_period         = int(settings.get('atr_period', 14))
        self.atr_multiplier     = float(settings.get('atr_multiplier', 1.0))
        self.trend_min_bricks   = int(settings.get('trend_min_bricks', 3))
        self.reversal_bricks    = int(settings.get('reversal_bricks', 2))
        self.vol_filter_enabled = bool(settings.get('vol_filter_enabled', True))
        self.min_vol_ratio      = float(settings.get('min_vol_ratio', 1.2))

    def _compute_brick_size(self, df: pd.DataFrame) -> float:
        if 'atr' in df.columns:
            atr_val = df['atr'].dropna().mean()
        else:
            hl = df['high'] - df['low']
            atr_val = hl.mean()
        brick = atr_val * self.atr_multiplier
        return max(brick, 1e-10)

    def _build_renko(self, df: pd.DataFrame, brick_size: float) -> list:
        """
        Baut Renko-Bricks aus OHLCV.
        Gibt Liste von Dicts zurück: direction (+1 up / -1 down), timestamp, candle_idx
        """
        bricks = []
        if df.empty or brick_size <= 0:
            return bricks

        closes     = df['close'].values
        timestamps = df.index.tolist()
        has_vol    = 'volume' in df.columns
        volumes    = df['volume'].values if has_vol else np.zeros(len(df))

        current_base = closes[0]

        for i in range(len(closes)):
            ts    = timestamps[i]
            close = closes[i]
            vol   = volumes[i]

            while close >= current_base + brick_size:
                bricks.append({
                    'direction':  1,
                    'timestamp':  ts,
                    'candle_idx': i,
                    'volume':     vol,
                })
                current_base += brick_size

            while close <= current_base - brick_size:
                bricks.append({
                    'direction':  -1,
                    'timestamp':  ts,
                    'candle_idx': i,
                    'volume':     vol,
                })
                current_base -= brick_size

        return bricks

    def process_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Verarbeitet OHLCV und fügt 'renko_signal' Spalte hinzu.
          1  = LONG-Signal
         -1  = SHORT-Signal
          0  = kein Signal
        """
        if df.empty:
            return df

        df = df.copy()
        df['renko_signal'] = 0

        brick_size = self._compute_brick_size(df)
        bricks     = self._build_renko(df, brick_size)

        min_window = self.trend_min_bricks + self.reversal_bricks
        if len(bricks) < min_window:
            return df

        volumes_arr = df['volume'].values if 'volume' in df.columns else None

        signal_map = {}

        for i in range(min_window - 1, len(bricks)):
            reversal_slice = bricks[i - self.reversal_bricks + 1: i + 1]
            trend_slice    = bricks[i - self.reversal_bricks - self.trend_min_bricks + 1:
                                    i - self.reversal_bricks + 1]

            rev_dirs   = [b['direction'] for b in reversal_slice]
            trend_dirs = [b['direction'] for b in trend_slice]

            signal_ts   = reversal_slice[-1]['timestamp']
            signal_cidx = reversal_slice[-1]['candle_idx']

            # LONG: trend war DOWN, reversal ist UP
            if all(d == 1 for d in rev_dirs) and all(d == -1 for d in trend_dirs):
                if self._passes_volume_filter(volumes_arr, signal_cidx):
                    signal_map[signal_ts] = 1

            # SHORT: trend war UP, reversal ist DOWN
            elif all(d == -1 for d in rev_dirs) and all(d == 1 for d in trend_dirs):
                if self._passes_volume_filter(volumes_arr, signal_cidx):
                    signal_map[signal_ts] = -1

        for ts, sig in signal_map.items():
            if ts in df.index:
                df.loc[ts, 'renko_signal'] = sig

        return df

    def _passes_volume_filter(self, volumes_arr, candle_idx: int) -> bool:
        if not self.vol_filter_enabled or volumes_arr is None:
            return True
        start = max(0, candle_idx - 19)
        window = volumes_arr[start: candle_idx + 1]
        if len(window) < 5:
            return True
        vol_avg = window.mean()
        cur_vol = volumes_arr[candle_idx]
        if vol_avg <= 0:
            return True
        return cur_vol >= vol_avg * self.min_vol_ratio

    def get_last_swing_sl(self, df: pd.DataFrame, side: str) -> float | None:
        """
        Berechnet SL-Preis basierend auf letztem Renko-Swing-High/-Low.
        side='long'  → SL = letztes Swing-Low  (tiefster Punkt der letzten DOWN-Sequenz)
        side='short' → SL = letztes Swing-High (höchster Punkt der letzten UP-Sequenz)
        Gibt None zurück wenn kein valider Swing gefunden.
        """
        brick_size = self._compute_brick_size(df)
        bricks     = self._build_renko(df, brick_size)
        if not bricks:
            return None

        directions = [b['direction'] for b in bricks]

        if side == 'long':
            # Suche letzten DOWN-Block → sein Ende = letzter DOWN-Brick-Close
            i = len(directions) - 1
            while i >= 0 and directions[i] != -1:
                i -= 1
            if i < 0:
                return None
            # Brick-Close des letzten DOWN-Bricks = current_base nach diesem Brick
            # Wir berechnen den Preis des letzten DOWN-Swing-Low approximativ
            # Nehme stattdessen das Minimum der letzten 3 Candles als einfache Näherung
            n_candles = min(5, len(df))
            return float(df['low'].iloc[-n_candles:].min())

        else:  # short
            i = len(directions) - 1
            while i >= 0 and directions[i] != 1:
                i -= 1
            if i < 0:
                return None
            n_candles = min(5, len(df))
            return float(df['high'].iloc[-n_candles:].max())
