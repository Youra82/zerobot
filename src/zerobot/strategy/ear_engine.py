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
        self.sl_bricks_back   = int(  settings.get('sl_bricks_back',   1))

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

    @staticmethod
    def candle_entropy_vectorized(o, h, l, c) -> np.ndarray:
        """Vektorisierte Variante von _candle_entropy fuer ganze Arrays --
        bit-identisch zur skalaren Version (verifiziert), aber ohne
        Python-Schleife. Fuer inkrementelle Fortsetzung einer laufenden
        Kette, wo nur ein kleines Pufferfenster neu berechnet werden muss."""
        hl  = h - l
        eps = 1e-10
        safe_hl = np.where(hl < 1e-12, 1.0, hl)
        pb = np.clip((c - l) / safe_hl, eps, 1 - eps)
        ps = np.clip((h - c) / safe_hl, eps, 1 - eps)
        s  = pb + ps
        pb = pb / s
        ps = ps / s
        ent = -pb * np.log2(pb) - ps * np.log2(ps)
        return np.where(hl < 1e-12, 1.0, ent)

    def _build_bricks(self, df: pd.DataFrame,
                      init_lc: float = None,
                      init_direction: str = None,
                      precomputed_H_roll=None) -> list:
        """
        Baut EAR-Bricks aus OHLCV. Gibt Liste von Dicts zurueck.

        init_lc / init_direction: persistierter State aus vorherigem Lauf.
        Ohne diese Parameter startet die Berechnung bei closes[0] (pfadabhaengig).
        Mit gespeichertem State ist die Brick-Struktur reproduzierbar.

        precomputed_H_roll: optional vorberechnetes geglaettetes Entropie-Array
        (gleiche Laenge wie df). Fuer inkrementelle Fortsetzung einer bereits
        laufenden Kette (trade_manager.py) -- dort wird H_roll aus einem
        kleinen Puffer-Fenster VOR den neuen Kerzen berechnet, damit das
        Rolling-Mean an der Nahtstelle korrekt ist, ohne die Pufferkerzen
        selbst nochmal durch die Brick-Konstruktion laufen zu lassen (das
        wuerde bereits verarbeitete Bricks doppelt erzeugen). Ohne dieses
        Argument identisch zum bisherigen Verhalten.
        """
        n = len(df)
        min_n = 1 if init_lc is not None else 2
        if n < min_n:
            return []

        closes = df['close'].values
        highs  = df['high'].values
        lows   = df['low'].values
        opens  = df['open'].values
        atrs   = df['atr'].values if 'atr' in df.columns else np.full(n, np.nan)

        if precomputed_H_roll is not None:
            H_roll = np.asarray(precomputed_H_roll)
        else:
            # Entropie + geglättetes H
            H_raw  = np.array([self._candle_entropy(opens[i], highs[i], lows[i], closes[i])
                                for i in range(n)])
            H_roll = pd.Series(H_raw).rolling(self.h_window, min_periods=1).mean().values

        bricks    = []
        lc        = init_lc        if init_lc        is not None else closes[0]
        direction = init_direction if init_direction is not None else None

        # Ohne expliziten Anker dient closes[0] nur als Referenzpunkt (Start bei i=1).
        # MIT explizitem Anker (init_lc) ist bereits jede Kerze ab i=0 gegen den Anker
        # zu pruefen -- sonst wuerde bei inkrementeller Fortsetzung (trade_manager.py:
        # update_brick_chain) systematisch genau die neueste Kerze jedes Batches
        # uebersprungen (bei typischerweise 1 neuer Kerze pro Cron-Lauf: JEDE Kerze).
        start_i = 0 if init_lc is not None else 1

        for i in range(start_i, n):
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

        # Startindex garantiert, dass bei jedem Signal mindestens sl_bricks_back
        # Bricks davor existieren -> bidx - sl_bricks_back ist nie negativ,
        # Backtester/Live-Bot brauchen dafür keinen Fallback.
        sig_map = {}   # candle_idx -> (signal_val, H)
        start_i = max(self.trend_min_bricks - 1, self.sl_bricks_back)
        for i in range(start_i, len(bdf)):
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
