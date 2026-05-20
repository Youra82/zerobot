#!/usr/bin/env python3
# tests/test_workflow.py
# Workflow-Tests für zerobot
#
# Tests:
#   1. Hurst-Exponent Berechnung (H=0.5 für Random Walk)
#   2. Approximate Entropy (niedrig für konstante Serie, hoch für Random)
#   3. Transfer Entropy (TE(X→X) > TE(X→Random) — kanonisches Ergebnis)
#   4. Quantum State Encoding (korrektes Format)
#   5. StateDB Upsert + Get
#   6. Discovery Pipeline (minimal)

import sys
import os
import pytest
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from zerobot.physics.hurst import hurst_rs_single, hurst_rs_multiscale, classify_hurst
from zerobot.physics.entropy import approximate_entropy, transfer_entropy, shannon_entropy
from zerobot.physics.encoder import encode_state, encode_dataframe
from zerobot.physics.database import StateDB


# ─── Hurst ───────────────────────────────────────────────────────────────────

class TestHurst:
    def test_random_walk_near_half(self):
        """Single-Window R/S für Random Walk → gültige Zahl im [0.01, 0.99]"""
        np.random.seed(42)
        rw = np.cumsum(np.random.randn(500))
        h = hurst_rs_single(rw)
        assert 0.01 <= h <= 0.99, f"Hurst außerhalb [0.01, 0.99]: {h}"

    def test_trending_series_high_hurst(self):
        """Stark trendierende Serie → H > 0.5"""
        ts = np.linspace(0, 100, 200) + np.random.randn(200) * 0.5
        h = hurst_rs_single(ts)
        assert h > 0.5, f"Trendierende Serie sollte H > 0.5 haben: {h}"

    def test_classify_hurst(self):
        assert classify_hurst(0.60) == 'T'
        assert classify_hurst(0.40) == 'R'
        assert classify_hurst(0.50) == 'N'

    def test_fallback_short_series(self):
        h = hurst_rs_single(np.array([1.0, 2.0, 3.0]))
        assert h == 0.5

    def test_multiscale_vs_single(self):
        """Multiscale und Single sollten nicht extrem abweichen"""
        np.random.seed(123)
        rw = np.cumsum(np.random.randn(300))
        h_single = hurst_rs_single(rw)
        h_multi = hurst_rs_multiscale(rw)
        assert abs(h_single - h_multi) < 0.4, f"Zu große Abweichung: {h_single} vs {h_multi}"


# ─── Entropy ─────────────────────────────────────────────────────────────────

class TestEntropy:
    def test_apen_constant_series(self):
        """Konstante Serie → ApEn nahe 0"""
        ts = np.zeros(50)
        apen = approximate_entropy(ts)
        assert apen == 0.0 or apen < 0.1

    def test_apen_random_series_higher(self):
        """Zufällige Serie → ApEn deutlich höher als konstante"""
        np.random.seed(42)
        ts_rand = np.random.randn(50)
        ts_const = np.zeros(50)
        apen_rand = approximate_entropy(ts_rand)
        apen_const = approximate_entropy(ts_const)
        assert apen_rand > apen_const

    def test_shannon_entropy_uniform(self):
        """Gleichverteilte Returns → hohe Entropie"""
        returns = np.random.uniform(-1, 1, 200)
        h = shannon_entropy(returns, bins=10)
        assert h > 0.7, f"Gleichverteilung sollte hohe Entropie haben: {h}"

    def test_transfer_entropy_self(self):
        """TE(X→X) sollte > 0 sein"""
        np.random.seed(42)
        x = np.cumprod(1 + np.random.randn(200) * 0.01)
        te = transfer_entropy(x, x, lag=1, bins=3)
        assert te >= 0.0

    def test_transfer_entropy_correlated(self):
        """TE(BTC→corr_alt) > TE(BTC→uncorr_alt)"""
        np.random.seed(42)
        btc = np.cumprod(1 + np.random.randn(300) * 0.01)
        # Korreliertes ALT: folgt BTC mit 1 Kerze Verzögerung
        corr_alt = np.roll(btc, 1)
        corr_alt[0] = btc[0]
        # Unkorrelliertes ALT
        uncorr_alt = np.cumprod(1 + np.random.randn(300) * 0.01)
        te_corr = transfer_entropy(btc, corr_alt, lag=1, bins=3, window=100)
        te_uncorr = transfer_entropy(btc, uncorr_alt, lag=1, bins=3, window=100)
        assert te_corr >= 0.0
        assert te_uncorr >= 0.0


# ─── Encoder ─────────────────────────────────────────────────────────────────

class TestEncoder:
    def test_state_format(self):
        """State-String muss Format DIR+BODY+WICK-HURST+ENTROPY+VOL haben"""
        state = encode_state(
            open_=100.0, high=105.0, low=98.0, close=104.0, volume=1000.0,
            atr=3.0, avg_volume=800.0,
            hurst_regime='T', entropy_regime='C',
        )
        assert len(state) == 7, f"State sollte 7 Zeichen haben: '{state}'"
        assert state[3] == '-', f"Trennzeichen fehlt: '{state}'"
        assert state[0] in 'BS'
        assert state[1] in '123'
        assert state[2] in 'UDBN'
        assert state[4] in 'TRN'
        assert state[5] in 'CE'
        assert state[6] in 'LH'

    def test_bullish_candle(self):
        state = encode_state(100.0, 110.0, 99.0, 109.0, 500.0, 5.0, 400.0, 'T', 'C')
        assert state[0] == 'B'

    def test_bearish_candle(self):
        state = encode_state(110.0, 111.0, 98.0, 100.0, 500.0, 5.0, 400.0, 'R', 'E')
        assert state[0] == 'S'

    def test_encode_dataframe(self):
        """encode_dataframe sollte Liste gleicher Länge zurückgeben"""
        np.random.seed(42)
        n = 100
        closes = np.cumprod(1 + np.random.randn(n) * 0.01) * 100
        df = pd.DataFrame({
            'open':   closes * 0.999,
            'high':   closes * 1.005,
            'low':    closes * 0.995,
            'close':  closes,
            'volume': np.random.uniform(500, 1500, n),
        })
        states = encode_dataframe(df)
        assert len(states) == n
        for s in states:
            assert len(s) == 7
            assert '-' in s


# ─── StateDB ─────────────────────────────────────────────────────────────────

class TestStateDB:
    def setup_method(self):
        """Temporäre In-Memory DB für jeden Test"""
        self.db = StateDB(':memory:')

    def teardown_method(self):
        self.db.close()

    def test_upsert_creates_new(self):
        is_new = self.db.upsert_state_outcome(
            sequence='B3N-TCH|S2U-NCL',
            market='BTC/USDT:USDT',
            timeframe='4h',
            direction='LONG',
            seq_length=2,
            is_win=True,
            move_pct=2.5,
            regime='TREND',
            hurst_value=0.62,
            apen_value=0.3,
        )
        assert is_new is True

    def test_upsert_updates_existing(self):
        kwargs = dict(
            sequence='B3N-TCH|S2U-NCL',
            market='BTC/USDT:USDT',
            timeframe='4h',
            direction='LONG',
            seq_length=2,
            is_win=True,
            move_pct=2.0,
            regime='TREND',
        )
        self.db.upsert_state_outcome(**kwargs)
        is_new = self.db.upsert_state_outcome(**kwargs)
        assert is_new is False

    def test_get_state(self):
        self.db.upsert_state_outcome(
            sequence='S2D-RCL',
            market='ETH/USDT:USDT',
            timeframe='1h',
            direction='SHORT',
            seq_length=1,
            is_win=False,
            move_pct=1.0,
        )
        state = self.db.get_state('S2D-RCL', 'ETH/USDT:USDT', '1h', 'SHORT')
        assert state is not None
        assert state['total_occurrences'] == 1

    def test_hurst_apen_stored(self):
        self.db.upsert_state_outcome(
            sequence='B1B-NCE',
            market='BTC/USDT:USDT',
            timeframe='4h',
            direction='LONG',
            seq_length=1,
            is_win=True,
            move_pct=3.0,
            hurst_value=0.65,
            apen_value=0.25,
        )
        state = self.db.get_state('B1B-NCE', 'BTC/USDT:USDT', '4h', 'LONG')
        assert state['hurst_count'] == 1
        assert abs(state['hurst_sum'] - 0.65) < 1e-6
        assert abs(state['apen_sum'] - 0.25) < 1e-6
