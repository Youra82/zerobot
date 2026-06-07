# src/zerobot/strategy/ear_engine.py
# Entropy-Adaptive Renko (EAR) — Signal-Generator
#
# Strategie-Kern:
#   1. Shannon-Entropie H pro Kerze: Chaos (H->1) vs. Ordnung (H->0)
#   2. EAR-Bricks: Brick-Size = close × base_pct × (1 + k_entropy × H_rolling)
#      -> kleine Bricks im Trend, grosse im Chaos
#   3. Signal: Entropy Squeeze — nach N chaos-Bricks (H > chaos_h_min)
#      sinkt H unter chaos_h_min × squeeze_ratio → Ordnung kehrt zurück
#   4. Richtung: Richtung des Squeeze-Bricks (up=long, down=short)

import numpy as np
import pandas as pd
import warnings

warnings.simplefilter(action='ignore', category=FutureWarning)


class EAREngine:
    """
    Konvertiert OHLCV-Kerzen zu Entropy-Adaptive Renko Bricks
    und erkennt Entropy-Squeeze-Signale.
    """

    def __init__(self, settings: dict):
        self.base_pct         = float(settings.get('base_pct',         0.004))
        self.k_entropy        = float(settings.get('k_entropy',        0.8))
        self.h_window         = int(  settings.get('h_window',         10))
        self.trend_min_bricks = int(  settings.get('trend_min_bricks', 3))

    @staticmethod
    def _candle_entropy(o, h, l, c) -> float:
        hl = h - l
        if hl < 1e-12:
            return 1.0
        eps = 1e-10
        pb  = float(np.clip((c - l) / hl, eps, 1 - eps))
        ps  = float(np.clip((h - c) / hl, eps, 1 - eps))
        s   = pb + ps
        pb /= s; ps /= s
        return float(-pb * np.log2(pb) - ps * np.log2(ps))

    def _build_bricks(self, df: pd.DataFrame,
                      init_lc: float = None,
                      init_direction: str = None) -> list:
        """
        Baut EAR-Bricks aus OHLCV. Gibt Liste von Dicts zurueck.

        init_lc / init_direction: persistierter State aus vorherigem Lauf.
        Ohne diese Parameter startet die Berechnung bei closes[0] (pfadabhaengig).
        Mit gespeichertem State ist die Brick-Struktur reproduzierbar.
        """
        n = len(df)
        if n < 2:
            return []

        closes = df['close'].values
        highs  = df['high'].values
        lows   = df['low'].values
        opens  = df['open'].values
        atrs   = df['atr'].values if 'atr' in df.columns else np.full(n, np.nan)

        # Entropie + geglättetes H
        H_raw  = np.array([self._candle_entropy(opens[i], highs[i], lows[i], closes[i])
                            for i in range(n)])
        H_roll = pd.Series(H_raw).rolling(self.h_window, min_periods=1).mean().values

        bricks    = []
        lc        = init_lc        if init_lc        is not None else closes[0]
        direction = init_direction if init_direction is not None else None

        for i in range(1, n):
            H     = float(H_roll[i])
            bs    = lc * self.base_pct * (1.0 + self.k_entropy * H)
            price = closes[i]
            atr   = float(atrs[i]) if not np.isnan(atrs[i]) else np.nan

            if direction is None:
                if price >= lc + bs:
                    direction = 'up'
                elif price <= lc - bs:
                    direction = 'down'
                else:
                    continue

            if direction == 'up':
                while price >= lc + bs:
                    nc = lc + bs
                    bricks.append({'candle_idx': i, 'direction': 'up',
                                   'H': H, 'atr': atr, 'close': nc})
                    lc = nc
                    bs = lc * self.base_pct * (1.0 + self.k_entropy * H)
                if price <= lc - 2 * bs:
                    direction = 'down'
                    while price <= lc - bs:
                        nc = lc - bs
                        bricks.append({'candle_idx': i, 'direction': 'down',
                                       'H': H, 'atr': atr, 'close': nc})
                        lc = nc
                        bs = lc * self.base_pct * (1.0 + self.k_entropy * H)
            elif direction == 'down':
                while price <= lc - bs:
                    nc = lc - bs
                    bricks.append({'candle_idx': i, 'direction': 'down',
                                   'H': H, 'atr': atr, 'close': nc})
                    lc = nc
                    bs = lc * self.base_pct * (1.0 + self.k_entropy * H)
                if price >= lc + 2 * bs:
                    direction = 'up'
                    while price >= lc + bs:
                        nc = lc + bs
                        bricks.append({'candle_idx': i, 'direction': 'up',
                                       'H': H, 'atr': atr, 'close': nc})
                        lc = nc
                        bs = lc * self.base_pct * (1.0 + self.k_entropy * H)

        return bricks

    def process_dataframe(self, df: pd.DataFrame,
                          init_lc: float = None,
                          init_direction: str = None) -> pd.DataFrame:
        """
        Haupt-Methode: verarbeitet OHLCV-DataFrame, fügt 'ear_signal' Spalte hinzu.
        ear_signal: 1=Long-Signal, -1=Short-Signal, 0=kein Signal

        init_lc / init_direction: optionaler persistierter Brick-State.
        """
        df = df.copy()
        df['ear_signal'] = 0
        df['ear_H']      = np.nan

        bricks = self._build_bricks(df, init_lc=init_lc, init_direction=init_direction)
        if len(bricks) < self.trend_min_bricks:
            return df

        bdf = pd.DataFrame(bricks)

        sig_map = {}   # candle_idx -> (signal_val, H)
        for i in range(self.trend_min_bricks - 1, len(bdf)):
            window_dirs = [bdf.iloc[j]['direction']
                           for j in range(i - self.trend_min_bricks + 1, i + 1)]
            if all(d == 'up'   for d in window_dirs):
                sig = 1
            elif all(d == 'down' for d in window_dirs):
                sig = -1
            else:
                continue
            cidx = int(bdf.iloc[i]['candle_idx'])
            sig_map[cidx] = (sig, float(bdf.iloc[i]['H']))

        ear_signal_col = df.columns.get_loc('ear_signal')
        ear_h_col      = df.columns.get_loc('ear_H')
        for cidx, (sig, h_val) in sig_map.items():
            if 0 <= cidx < len(df):
                df.iloc[cidx, ear_signal_col] = sig
                df.iloc[cidx, ear_h_col]      = h_val

        return df
