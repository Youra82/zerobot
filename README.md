# zerobot — Quantum State Trading Bot

Physics-informed algorithmic trading bot for cryptocurrency perpetual futures on Bitget.
Pattern recognition using Hurst Exponent, Approximate Entropy and Transfer Entropy.
Architecture inspired by [dnabot](https://github.com/Youra82/dnabot), extended with information-theoretic market physics.

---

## Concept

Instead of encoding candles as simple gene-strings (dnabot), zerobot encodes every candle as a **7-dimensional Quantum State** that captures both price structure and the physical regime of the market at that moment:

```
B3N-TCH
│││ │││
│││ ││└── Volume:   H=high / L=low (vs. 20-period MA)
│││ │└─── Entropy:  C=calm (low ApEn) / E=excited (high ApEn)
│││ └──── Hurst:    T=trending (H>0.55) / R=reverting (H<0.45) / N=neutral
│││
│││ (separator)
││└────── Wick:     U=upper / D=lower / B=both / N=none
│└─────── Body:     1=small / 2=medium / 3=large (relative to ATR)
└──────── Direction: B=bullish / S=bearish
```

Sequences of 3–5 states form patterns. Each pattern is stored in SQLite with win/loss statistics, Hurst and ApEn values at the time it occurred. The **Evolver** scores patterns using:

```
score = winrate × avg_move_pct × log(1 + effective_occ)

effective_occ = occ × decay × entropy_bonus × hurst_bonus

entropy_bonus = 1.0 + 0.30 × (1 − mean_ApEn)   # calm markets → reliable patterns
hurst_bonus   = 1.0 + 0.20 × |mean_Hurst − 0.5|  # extreme Hurst → structural edge
decay         = exp(−age_days / half_life)          # older patterns lose weight
```

---

## Architecture

```
zerobot/
├── src/zerobot/
│   ├── physics/
│   │   ├── hurst.py        # R/S Analysis, rolling Hurst exponent
│   │   ├── entropy.py      # ApEn, Shannon Entropy, Transfer Entropy (BTC→ALT)
│   │   ├── encoder.py      # Candle → 7-char quantum state string
│   │   ├── database.py     # SQLite StateDB (WAL mode, Hurst/ApEn per pattern)
│   │   ├── discovery.py    # Sliding-window pattern mining
│   │   └── evolver.py      # Physics-informed scoring + activation
│   ├── strategy/
│   │   ├── signal_logic.py # 4-gate signal filter
│   │   └── run.py          # Live trading entry point
│   ├── analysis/
│   │   └── backtester.py   # Historical simulation engine
│   └── utils/
│       ├── exchange.py     # Bitget CCXT wrapper
│       ├── telegram.py     # Telegram notifications
│       ├── guardian.py     # Error recovery decorator
│       └── trade_manager.py # Order placement + self-learning
├── scan_and_learn.py       # Pattern discovery pipeline
├── run_backtest.py         # 70/30 train/test backtest
├── run_optimizer.py        # Optuna physics parameter optimizer
├── master_runner.py        # Subprocess orchestrator
├── settings.json           # Bot configuration
└── tests/
    └── test_workflow.py    # 18 unit tests
```

---

## Signal Generation — 4 Gates

Only if all 4 gates pass does a trade get placed:

1. **Quantum State Match** — last 3–5 candles form a known active pattern in the DB
2. **Hurst Regime Alignment** — current market regime matches the pattern's learned regime
3. **Entropy Filter** — current ApEn < `max_apen_for_trade` (market not too chaotic)
4. **Transfer Entropy Boost** — if BTC leads the target (TE > threshold), score × 1.25

---

## Backtest Results (BTC/USDT:USDT 4h, 730 days)

| Period | Dates | Trades | Win-Rate | PnL | Max DD | Calmar |
|--------|-------|--------|----------|-----|--------|--------|
| Train 70% | May 2024 – Oct 2025 | 126 | 25.4% | +30.1% | 14.1% | 2.13 |
| **Test 30%** | **Oct 2025 – May 2026** | **44** | **29.5%** | **+20.7%** | **9.6%** | **2.16** |

Optimized by Optuna on OOS test period (anti-overfitting). Capital: 50 USDT, 1% risk/trade, 5× leverage, R:R = 1:3.5.
Win rate is low (29%) but expected value is strongly positive: `0.295 × 3.5 − 0.705 × 1 = +0.327` per trade unit.

---

## Setup

```bash
git clone https://github.com/Youra82/zerobot.git
cd zerobot
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Create secret.json (see template below)
cp secret.json.example secret.json   # or create manually
```

### secret.json format

```json
{
  "zerobot": [
    {
      "api_key": "YOUR_BITGET_API_KEY",
      "api_secret": "YOUR_BITGET_SECRET",
      "passphrase": "YOUR_BITGET_PASSPHRASE",
      "telegram_bot_token": "YOUR_BOT_TOKEN",
      "telegram_chat_id": "YOUR_CHAT_ID"
    }
  ]
}
```

---

## Usage

### 1. Pattern Discovery (build the state database)

```bash
.venv/bin/python3 scan_and_learn.py
# or for a specific symbol/timeframe:
.venv/bin/python3 scan_and_learn.py --symbol BTC/USDT:USDT --timeframe 4h
```

### 2. Optimize Physics Parameters (Optuna, OOS-validated)

```bash
.venv/bin/python3 run_optimizer.py --trials 50 --capital 50
# Optimizes: min_score, max_apen_for_trade, rr_ratio, te_threshold
# Objective: maximize Calmar ratio on 30% out-of-sample test period
```

### 3. Validate with Backtest

```bash
.venv/bin/python3 run_backtest.py --capital 50
# Shows 70/30 train/test split comparison with overfitting warnings
```

### 4. Start Live Trading

```bash
.venv/bin/python3 master_runner.py
# or single strategy:
.venv/bin/python3 src/zerobot/strategy/run.py --symbol BTC/USDT:USDT --timeframe 4h
```

---

## Configuration (settings.json)

```json
{
  "physics_settings": {
    "sequence_lengths": [3, 4, 5],
    "min_score": 0.05,
    "min_winrate": 0.45,
    "half_life_days": 180.0,
    "max_apen_for_trade": 2.4,
    "transfer_entropy_enabled": true,
    "te_threshold": 0.045,
    "te_boost_factor": 1.25,
    "te_reference_symbol": "BTC/USDT:USDT"
  },
  "risk_settings": {
    "risk_per_entry_pct": 1.0,
    "leverage": 5,
    "rr_ratio": 3.5,
    "trailing_callback_rate_pct": 1.0
  },
  "live_trading_settings": {
    "active_strategies": [
      {"symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": true}
    ]
  }
}
```

---

## VPS Deployment

```bash
# Initial setup
git clone https://github.com/Youra82/zerobot.git
cd zerobot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# create secret.json

# Build pattern database (first time, ~10 min)
.venv/bin/python3 scan_and_learn.py

# Optimize parameters
.venv/bin/python3 run_optimizer.py --trials 50 --capital 50 --auto-write

# Start bot
.venv/bin/python3 master_runner.py

# Update (preserves secret.json and quantum.db)
bash update.sh
```

---

## Tests

```bash
pytest tests/test_workflow.py -v
# 18 passed — Hurst, Entropy, Encoder, StateDB
```

---

## Dependencies

- `ccxt` — Bitget exchange API
- `numpy` / `pandas` — data processing
- `ta` — technical indicators (ATR)
- `optuna` — Bayesian hyperparameter optimization
- `requests` — Telegram notifications

---

## Physics Background

| Metric | Formula | Meaning |
|--------|---------|---------|
| **Hurst Exponent** | R/S Analysis | H>0.55 = trending, H<0.45 = mean-reverting, H≈0.5 = random walk |
| **Approximate Entropy** | Template matching | Low ApEn = predictable market = patterns more reliable |
| **Transfer Entropy** | TE(X→Y) = H(Y\|past Y) − H(Y\|past Y, past X) | Directional information flow from BTC to altcoin |
| **Shannon Entropy** | −Σ p(x) log p(x) | Normalized entropy of return distribution |

---

*Built as an evolution of [dnabot](https://github.com/Youra82/dnabot) — same architecture, richer physics-informed state encoding.*
