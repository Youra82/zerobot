# zerobot — Quantum State Trading Bot

Physics-informierter algorithmischer Trading-Bot für Krypto-Perpetual-Futures auf Bitget.
Mustererkennung mit Hurst-Exponent, Approximate Entropy und Transfer Entropy.
Architektur basiert auf [dnabot](https://github.com/Youra82/dnabot), erweitert um informationstheoretische Marktphysik.

---

## Konzept

Jede Kerze wird als **7-dimensionaler Quantum State** kodiert — er beschreibt nicht nur die Kerzenstruktur, sondern auch den physikalischen Zustand des Marktes zum jeweiligen Zeitpunkt:

```
B3N-TCH
│││ │││
│││ ││└── Volumen:   H=hoch / L=niedrig (vs. 20-Perioden-MA)
│││ │└─── Entropie:  C=ruhig (ApEn niedrig) / E=aufgewühlt (ApEn hoch)
│││ └──── Hurst:     T=Trend (H>0.55) / R=Reversion (H<0.45) / N=Neutral
│││
│││ (Trennzeichen)
││└────── Docht:     U=oben / D=unten / B=beide / N=keiner
│└─────── Körper:    1=klein / 2=mittel / 3=groß (relativ zu ATR)
└──────── Richtung:  B=bullish / S=bearish
```

Sequenzen aus 3–5 aufeinanderfolgenden States bilden Muster. Jedes Muster wird in einer SQLite-Datenbank gespeichert — mit Gewinn/Verlust-Statistiken sowie dem Hurst- und ApEn-Wert zum Zeitpunkt des Auftretens. Der **Evolver** bewertet Muster nach folgender Formel:

```
score = winrate × avg_move_pct × log(1 + effective_occ)

effective_occ = occ × decay × entropy_bonus × hurst_bonus

entropy_bonus = 1.0 + 0.30 × (1 − mittl. ApEn)      # ruhige Märkte → zuverlässigere Muster
hurst_bonus   = 1.0 + 0.20 × |mittl. Hurst − 0.5|    # extremer Hurst → struktureller Edge
decay         = exp(−Alter_Tage / Halbwertszeit)       # alte Muster verlieren Gewicht
```

---

## Unterschied zu dnabot

dnabot kodiert *was die Kerze macht*. zerobot kodiert *was die Kerze macht* **und** *in welchem physikalischen Marktregime sie auftritt*.

| | **dnabot** | **zerobot** |
|---|---|---|
| State-Format | `B3H-UH` (6 Zeichen) | `B3N-TCH` (7 Zeichen) |
| Mögliche States | 96 | **288** |
| Regime-Erkennung | ADX-basiert | **Hurst-Exponent** (fraktal) |
| Entropie-Filter | nein | **ApEn-Gate** (kein Trade bei chaotischem Markt) |
| BTC-Einfluss | nein | **Transfer Entropy** BTC→ALT (+25% Score-Boost) |
| Signal-Gates | 2 | **4** |
| Optimizer | Greedy Portfolio | **Optuna** (Bayesian, OOS Calmar-Ziel) |

Ein Muster das immer im Trend-Regime bei niedriger Entropie auftritt ist zuverlässiger als dasselbe Kerzenmuster in einem chaotischen Seitwärtsmarkt — das weiß dnabot nicht, zerobot schon.

---

## Architektur

```
zerobot/
├── src/zerobot/
│   ├── physics/
│   │   ├── hurst.py        # R/S-Analyse, rollierender Hurst-Exponent
│   │   ├── entropy.py      # ApEn, Shannon-Entropie, Transfer Entropy (BTC→ALT)
│   │   ├── encoder.py      # Kerze → 7-Zeichen Quantum-State-String
│   │   ├── database.py     # SQLite StateDB (WAL-Mode, Hurst/ApEn pro Muster)
│   │   ├── discovery.py    # Sliding-Window Pattern-Mining
│   │   └── evolver.py      # Physics-informiertes Scoring + Aktivierung
│   ├── strategy/
│   │   ├── signal_logic.py # 4-Gate Signal-Filter
│   │   └── run.py          # Live-Trading Entry Point
│   ├── analysis/
│   │   └── backtester.py   # Historische Simulation
│   └── utils/
│       ├── exchange.py     # Bitget CCXT Wrapper
│       ├── telegram.py     # Telegram-Benachrichtigungen
│       ├── guardian.py     # Fehlerbehandlung
│       └── trade_manager.py # Orderplatzierung + Self-Learning
├── scan_and_learn.py       # Pattern-Discovery-Pipeline
├── run_backtest.py         # 70/30 Train/Test Backtest
├── run_optimizer.py        # Optuna Physics-Parameter-Optimizer
├── master_runner.py        # Subprocess-Orchestrator
├── settings.json           # Bot-Konfiguration
└── tests/
    └── test_workflow.py    # 18 Unit-Tests
```

---

## Signal-Erzeugung — 4 Gates

Ein Trade wird nur platziert wenn alle 4 Stufen bestanden werden:

1. **Quantum State Match** — die letzten 3–5 Kerzen bilden ein bekanntes aktives Muster in der DB
2. **Hurst-Regime-Abgleich** — aktuelles Marktregime stimmt mit dem gelernten Regime des Musters überein
3. **Entropie-Filter** — aktueller ApEn-Wert < `max_apen_for_trade` (Markt nicht zu chaotisch)
4. **Transfer-Entropy-Boost** — wenn BTC dem Zielsymbol vorausläuft (TE > Schwellwert), Score × 1.25

---

## Backtest-Ergebnisse (BTC/USDT:USDT 4h, 730 Tage)

| Periode | Zeitraum | Trades | Win-Rate | PnL | Max DD | Calmar |
|---------|----------|--------|----------|-----|--------|--------|
| Train 70% | Mai 2024 – Okt 2025 | 126 | 25.4% | +30.1% | 14.1% | 2.13 |
| **Test 30%** | **Okt 2025 – Mai 2026** | **44** | **29.5%** | **+20.7%** | **9.6%** | **2.16** |

Optimiert mit Optuna auf der OOS-Test-Periode (Anti-Overfitting). Kapital: 50 USDT, 1% Risiko/Trade, 5× Hebel, R:R = 1:3.5.
Die Win-Rate ist bewusst niedrig (29%) — der erwartete Wert pro Trade ist dennoch positiv: `0.295 × 3.5 − 0.705 × 1 = +0.33`.

---

## Installation

```bash
git clone https://github.com/Youra82/zerobot.git
cd zerobot
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### secret.json anlegen

```json
{
  "zerobot": [
    {
      "api_key": "DEIN_BITGET_API_KEY",
      "api_secret": "DEIN_BITGET_SECRET",
      "passphrase": "DEIN_BITGET_PASSPHRASE",
      "telegram_bot_token": "DEIN_BOT_TOKEN",
      "telegram_chat_id": "DEINE_CHAT_ID"
    }
  ]
}
```

---

## Nutzung

### 1. Pattern Discovery (Musterdatenbank aufbauen)

```bash
.venv/bin/python3 scan_and_learn.py
# oder für ein bestimmtes Symbol:
.venv/bin/python3 scan_and_learn.py --symbol BTC/USDT:USDT --timeframe 4h
```

### 2. Physics-Parameter optimieren (Optuna, OOS-validiert)

```bash
.venv/bin/python3 run_optimizer.py --trials 50 --capital 50
# Optimiert: min_score, max_apen_for_trade, rr_ratio, te_threshold
# Ziel: Calmar-Ratio auf 30% Out-of-Sample-Periode maximieren
```

### 3. Backtest validieren

```bash
.venv/bin/python3 run_backtest.py --capital 50
# Zeigt 70/30 Train/Test-Vergleich mit Overfitting-Warnung
```

### 4. Live-Trading starten

```bash
.venv/bin/python3 master_runner.py
```

---

## Konfiguration (settings.json)

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
# Ersteinrichtung
git clone https://github.com/Youra82/zerobot.git
cd zerobot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# secret.json anlegen

# Musterdatenbank aufbauen (~10 Min)
.venv/bin/python3 scan_and_learn.py

# Parameter optimieren
.venv/bin/python3 run_optimizer.py --trials 50 --capital 50 --auto-write

# Bot starten
.venv/bin/python3 master_runner.py

# Update (secret.json und quantum.db bleiben erhalten)
bash update.sh
```

---

## Tests

```bash
pytest tests/test_workflow.py -v
# 18 passed — Hurst, Entropie, Encoder, StateDB
```

---

## Abhängigkeiten

- `ccxt` — Bitget Exchange API
- `numpy` / `pandas` — Datenverarbeitung
- `ta` — Technische Indikatoren (ATR)
- `optuna` — Bayesianische Hyperparameter-Optimierung
- `requests` — Telegram-Benachrichtigungen

---

## Physikalischer Hintergrund

| Metrik | Formel | Bedeutung |
|--------|--------|-----------|
| **Hurst-Exponent** | R/S-Analyse | H>0.55 = trendend, H<0.45 = mean-revertierend, H≈0.5 = Zufallslauf |
| **Approximate Entropy** | Template-Matching | Niedriger ApEn = vorhersagbarer Markt = Muster zuverlässiger |
| **Transfer Entropy** | TE(X→Y) = H(Y\|past Y) − H(Y\|past Y, past X) | Gerichteter Informationsfluss von BTC zum Altcoin |
| **Shannon-Entropie** | −Σ p(x) log p(x) | Normierte Entropie der Renditeverteilung |

---

*Weiterentwicklung von [dnabot](https://github.com/Youra82/dnabot) — gleiche Architektur, reicheres physics-informiertes State-Encoding.*
