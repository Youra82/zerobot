# zerobot — Quantum State Trading Bot

Ein selbstlernender Trading-Bot, der Marktbewegungen durch physikalische Zustandsvektoren analysiert.
Keine neuronalen Netze, keine Black-Box — deterministisches Pattern Discovery mit Informationstheorie.

> **Disclaimer:** Diese Software ist experimentell und dient ausschließlich Forschungszwecken.
> Der Handel mit Kryptowährungen birgt erhebliche finanzielle Risiken. Nutzung auf eigene Gefahr.

---

## Grundidee

Jede Kerze wird zu einem **Quantum State** komprimiert:

```
B3N-TCH
│││ │││
│││ ││└── Volumen:   H = hoch (über 20er-MA), L = niedrig
│││ │└─── Entropie:  C = ruhig (ApEn niedrig), E = aufgewühlt (ApEn hoch)
│││ └──── Hurst:     T = Trend (H>0.55), R = Reversion (H<0.45), N = Neutral
│││
│││ (Trennzeichen)
││└────── Docht:     U = oben, D = unten, B = beide, N = keiner
│└─────── Körper:    1 = klein (<30% ATR), 2 = mittel, 3 = groß (>80% ATR)
└──────── Richtung:  B = Bullish, S = Bearish
```

**288 mögliche Zustände** — kodiert nicht nur die Kerzenstruktur, sondern auch den physikalischen Zustand des Marktes.

Sequenzen aus 3–5 aufeinanderfolgenden States bilden ein **Quantum-Muster**:

```
"S1B-TCL | B1B-TCL | S3N-TCH"
   ↓
Dieses Muster erschien 5x in der Vergangenheit.
4x davon stieg der Kurs danach > 1%.
→ Winrate: 80.0% | Score: 5.54 | Hurst: 0.75 | ApEn: 0.15 | Status: AKTIV
```

Der Bot handelt nur, wenn ein solches Muster im Live-Markt erkannt wird — und nur wenn alle 4 Qualitätsgates bestanden werden.

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

---

## Architektur

```
zerobot/
├── scan_and_learn.py              # Haupt-Lernprozess (Discovery + Evolver)
├── master_runner.py               # Cronjob-Orchestrator für Live-Trading
├── run_pipeline.sh                # Vollständige Pipeline (Discovery → Backtest)
├── show_results.sh                # Interaktives Analyse- & Backtest-Menü
├── run_tests.sh                   # Pytest-Suite ausführen
├── run_backtest.py                # 70/30 Train/Test Backtest
├── run_optimizer.py               # Optuna Physics-Parameter-Optimizer
├── update.sh                      # Git-Update (sichert secret.json + quantum.db)
├── settings.json                  # Konfiguration
├── secret.json                    # API-Keys (nicht in Git)
│
└── src/zerobot/
    ├── physics/
    │   ├── hurst.py               # R/S-Analyse, rollierender Hurst-Exponent
    │   ├── entropy.py             # ApEn, Shannon-Entropie, Transfer Entropy
    │   ├── encoder.py             # Kerze → 7-Zeichen Quantum-State-String
    │   ├── database.py            # SQLite-Interface (Quantum State Library)
    │   ├── discovery.py           # Pattern-Mining aus Historien-Daten
    │   └── evolver.py             # Physics-Scoring + Aktivierung/Deaktivierung
    │
    ├── strategy/
    │   ├── signal_logic.py        # 4-Gate Signal-Filter → Trade-Signal
    │   └── run.py                 # Entry Point für eine Strategie
    │
    ├── analysis/
    │   └── backtester.py          # Historische Simulation (70/30 Split)
    │
    └── utils/
        ├── exchange.py            # Bitget CCXT Wrapper
        ├── trade_manager.py       # Entry/TP/SL + Self-Learning
        ├── telegram.py            # Telegram-Benachrichtigungen
        └── guardian.py            # Crash-Schutz Decorator
```

---

## Wie das System lernt

### Phase 1 — Discovery (`scan_and_learn.py`)

```
Historische Daten (2 Jahre OHLCV)
    ↓
Alle Kerzen → Quantum States kodieren (Hurst + ApEn werden eingebettet)
    ↓
Sliding Window (seq_len = 3, 4, 5)
    ↓
Für jedes Fenster: Was passierte danach? (strikt NACH dem Sequenz-Close)
  max_up > 1% UND max_up > max_down → LONG-Outcome
  max_down > 1% UND max_down > max_up → SHORT-Outcome
  Zusätzlich: Hurst + ApEn zum Zeitpunkt des Musters werden gespeichert
    ↓
States in SQLite speichern / aktualisieren
```

> Zukunfts-Kerzen werden ausschließlich nach dem Close der letzten Sequenz-Kerze bewertet
> (kein Lookahead-Bias).

### Phase 2 — Evolution (`evolver.py`)

Der Evolver bewertet jedes Muster **mit Physics-Bonussen**:

```
Für jedes Regime (TREND / REVERTING / NEUTRAL):
  effective_occ = occ × decay × entropy_bonus × hurst_bonus
  score = winrate × avg_move_pct × log(1 + effective_occ)

  entropy_bonus = 1.0 + 0.30 × (1 − mittl. ApEn)      [0.70 – 1.30]
  hurst_bonus   = 1.0 + 0.20 × |mittl. Hurst − 0.5|   [1.00 – 1.10]
  decay         = exp(−Alter_Tage / Halbwertszeit)

Ein Regime wird aktiviert wenn:
  - occ_regime  ≥ min_samples (statistisch belastbar)
  - winrate     ≥ 45%
  - score       ≥ 0.08

active_regimes = Liste der qualifizierenden Regime
```

### Phase 3 — Signal-Erzeugung (4 Gates)

```
Jeder Cronjob-Lauf:
  Gate 1: Letzte 3–5 Kerzen → Quantum State → DB-Abfrage (State Match)
  Gate 2: Hurst-Regime des Musters stimmt mit aktuellem Marktregime überein
  Gate 3: ApEn < max_apen_for_trade (Markt nicht zu chaotisch)
  Gate 4: Transfer Entropy BTC→ALT — falls TE > Schwellwert: Score × 1.25

  Wenn alle Gates bestanden:
  4. Entry: Market-Order (sofort bei Sequenz-Abschluss)
  5. SL: Low/High der Sequenz-Kerzen
  6. Trailing Stop: aktiviert bei R:R-Ratio, Callback 1% (Bitget nativ)

Nach Trade-Abschluss:
  → Self-Learning: Ergebnis + Hurst + ApEn in State-DB schreiben
  → Score wird für nächsten Evolver-Lauf aktualisiert
```

---

## Quantum State Datenbank

SQLite unter `artifacts/db/quantum.db`.
Eine Zeile pro State-Muster (eindeutig durch Sequenz + Markt + Timeframe + Richtung):

| Feld | Beispiel | Bedeutung |
|---|---|---|
| `state_id` | `a3f2b9c1...` | MD5-Hash (eindeutiger Schlüssel) |
| `sequence` | `S1B-TCL\|B1B-TCL\|S3N-TCH` | State-Sequenz |
| `market` | `BTC/USDT:USDT` | Handelspaar |
| `timeframe` | `4h` | Zeitrahmen |
| `direction` | `LONG` | Erwartete Richtung |
| `total_occurrences` | `5` | Wie oft dieses Muster in der History auftrat |
| `wins` | `4` | Wie oft danach die erwartete Bewegung kam |
| `avg_move_pct` | `3.35` | Durchschnittliche Preisbewegung in % |
| `score` | `5.54` | Bester Physics-Score |
| `active` | `1` | Vom Evolver freigegeben |
| `hurst_sum` / `hurst_count` | `3.76` / `5` | Hurst-Summe + Anzahl (für Durchschnitt) |
| `apen_sum` / `apen_count` | `0.76` / `5` | ApEn-Summe + Anzahl |
| `occ_trend` / `wins_trend` | `5` / `4` | Vorkommen + Wins im TREND-Regime |
| `active_regimes` | `["TREND"]` | Regime, in denen der State gehandelt wird |

---

## Konfiguration (`settings.json`)

```json
{
    "live_trading_settings": {
        "active_strategies": [
            { "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": false }
        ]
    },
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
    }
}
```

> **Automatische Ableitung:** Discovery-Parameter werden automatisch nach Timeframe gewählt:
>
> | Parameter | 1h | 4h | 1d |
> |---|---|---|---|
> | `history_days` | 365d | 730d | 1095d |
> | `discovery_horizon` | 24 Kerzen | 6 Kerzen | 3 Kerzen |
> | `move_threshold_pct` | 0.5% | 1.0% | 2.0% |
> | `min_samples` | 6 | 4 | 3 |

---

## Installation

#### 1. Projekt klonen

```bash
git clone https://github.com/Youra82/zerobot.git
cd zerobot
```

#### 2. Virtuelle Umgebung + Abhängigkeiten

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install optuna               # für run_optimizer.py
```

#### 3. API-Keys eintragen

```bash
nano secret.json
```

```json
{
    "zerobot": [
        {
            "name": "Main-Account",
            "apiKey": "DEIN_API_KEY",
            "secret": "DEIN_SECRET",
            "password": "DEIN_PASSPHRASE",
            "telegram_bot_token": "DEIN_BOT_TOKEN",
            "telegram_chat_id": "DEINE_CHAT_ID"
        }
    ]
}
```

---

## Workflow

#### 1. Coins und Timeframes einstellen

```bash
nano settings.json
```

```json
"active_strategies": [
    { "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": false }
]
```

#### 2. Quantum State Discovery starten (Pipeline)

```bash
./run_pipeline.sh
```

Die Pipeline lädt historische Daten, entdeckt Muster, bewertet sie und zeigt eine Backtest-Zusammenfassung. Dauert je nach Anzahl der Märkte 10–30 Minuten.

#### 3. Ergebnisse analysieren & Parameter optimieren

```bash
./show_results.sh
```

| Modus | Funktion |
|---|---|
| **1) Backtest 70/30** | Zeigt Train- und Test-Periode im direkten Vergleich mit Overfitting-Warnung. |
| **2) Backtest Test-only** | Nur die Out-of-Sample Testperiode (30%). |
| **3) Backtest Pair** | Einzelnes Symbol/Timeframe backtesten. |
| **4) Optimizer** | Optuna optimiert `min_score`, `max_apen_for_trade`, `rr_ratio`, `te_threshold` auf der OOS-Periode. |
| **5) Optimizer Auto-Write** | Wie 4), schreibt beste Parameter automatisch in `settings.json`. |
| **6) State Library** | Top-Patterns, Score-Verteilung und Statistiken aus der Quantum-DB. |

#### 4. Strategie live schalten

Nach der Optimierung (Modus 5 in `show_results.sh`) werden die optimalen Parameter automatisch in `settings.json` eingetragen. Alternativ manuell:

```bash
nano settings.json
```

```json
{ "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": true }
```

#### 5. Cronjob einrichten

```bash
crontab -e
```

```cron
# zerobot — alle 4 Stunden (bei Kerzenabschluss)
0 */4 * * * /usr/bin/flock -n /home/matola/zerobot/zerobot.lock \
    /bin/sh -c "cd /home/matola/zerobot && \
    .venv/bin/python3 master_runner.py >> logs/cron.log 2>&1"
```

---

## Tägliche Verwaltung & Wichtige Befehle

#### Logs ansehen

```bash
# Live mitverfolgen
tail -f logs/cron.log

# Nach Fehlern suchen
grep -i "ERROR" logs/cron.log

# Discovery-Log
tail -f logs/scan_and_learn.log

# Optimizer-Log
tail -f logs/optimizer.log

# Backtest-Log
tail -f logs/backtest.log
```

#### Manueller Start (Test)

```bash
cd ~/zerobot && .venv/bin/python3 master_runner.py
```

#### Discovery manuell starten

```bash
# Alle konfigurierten Pairs
./run_pipeline.sh

# Nur ein bestimmtes Pair
.venv/bin/python3 scan_and_learn.py --symbol BTC/USDT:USDT --timeframe 4h
```

#### Tests ausführen

```bash
./run_tests.sh
```

Führt alle Pytest-Tests aus (Sicherheitscheck vor dem Live-Betrieb).

#### Bot aktualisieren

```bash
./update.sh
```

Sichert automatisch `secret.json` und `artifacts/db/quantum.db` vor dem `git reset --hard`.

#### Quantum State Datenbank zurücksetzen

```bash
# Achtung: löscht alle erlernten Muster!
rm artifacts/db/quantum.db
./run_pipeline.sh
```

---

## Backtest-Ergebnisse (BTC/USDT:USDT 4h, 730 Tage)

| Periode | Zeitraum | Trades | Win-Rate | PnL | Max DD | Calmar |
|---------|----------|--------|----------|-----|--------|--------|
| Train 70% | Mai 2024 – Okt 2025 | 126 | 25.4% | +30.1% | 14.1% | 2.13 |
| **Test 30%** | **Okt 2025 – Mai 2026** | **44** | **29.5%** | **+20.7%** | **9.6%** | **2.16** |

Kapital: 50 USDT | 1% Risiko/Trade | 5× Hebel | R:R = 1:3.5 (Optuna-optimiert)

---

## Wichtige Regeln

- `secret.json` ist **nicht in Git** — wird von `update.sh` gesichert
- `artifacts/db/quantum.db` ist **nicht in Git** — bleibt nach Updates erhalten
- `artifacts/tracker/` ist **nicht in Git** — enthält den offenen Trade-Status pro Symbol
- Immer erst `./run_pipeline.sh` bevor Live-Trading aktiviert wird
- States mit weniger als 4 Samples (4h) werden grundsätzlich nicht gehandelt

---

## Coin & Timeframe Empfehlungen

zerobot ist eine **Quantum-State-Pattern-Strategie** — er kodiert Kerzen als physikalische Zustandsvektoren (z.B. `B3N-TCH`) und sucht in der Datenbank nach 3/4/5-Kerzen-Sequenzen mit statistisch valider Win-Rate und Physics-Alignment. Benötigt: Coins mit wiederkehrenden, lernbaren Kerzenmustern und ausreichend historische Daten für die Quantum-State-Datenbank.

### Effektive Zeitspannen der Sequenz-Fenster

| TF | 3-Kerzen-Sequenz | 5-Kerzen-Sequenz | Muster-Qualität | Geeignet |
|---|---|---|---|---|
| 15m | 45 Min | 1.25h | Noise-dominiert | ❌ |
| 30m | 1.5h | 2.5h | Marginal | ⚠️ |
| 1h | 3h | 5h | Intraday-Session | ✅ |
| **2h** | **6h** | **10h** | **Mehrere Sessions** | **✅✅** |
| **4h** | **12h** | **20h** | **Voller Handelstag** | **✅✅** |
| **6h** | **18h** | **30h** | **1.5 Tage — Swing** | **✅✅** |
| 1d | 3d | 5d | Wochen-Muster | ✅ |

Auf 15m/30m sind 3-5 Kerzen nur 45 Min bis 2.5h — zu kurz für statistisch bedeutsame wiederkehrende Muster. Ab 2h deckt eine Sequenz komplette Handelssessions ab. Die Quantum-State-Datenbank braucht außerdem ausreichend historische Kerzen für die Discovery-Phase.

### Coin-Eignung

| Coin | Kerzenmuster-Qualität | Wiederholbarkeit | DB-Datenbasis | Bewertung |
|---|---|---|---|---|
| **BTC** | Exzellent — institutionelle Muster | Sehr hoch durch globale Beobachtung | Längste Historie, beste Basis | ✅✅ Beste Wahl |
| **ETH** | Exzellent — klare, strukturierte Kerzen | Sehr hoch | Sehr gute Datenbasis | ✅✅ Sehr gut |
| **SOL** | Sehr gut — klare Richtungskerzen | Hoch | Gute Datenbasis ab 2020 | ✅ Gut |
| **BNB** | Gut — stabile, wiederholende Muster | Gut | Lange Datenbasis | ✅ Gut |
| **XRP** | Gut — klare Kerzenstruktur | Gut, besonders in Range-Phasen | Sehr lange Datenbasis | ✅ Gut |
| **AVAX** | Gut — ordentliche Kerzenformen | Mittel-hoch | Ausreichend ab 2020 | ✅ Gut |
| **LTC** | Gut — BTC-korreliert | Gut | Lange Datenbasis | ✅ Gut |
| **ADA** | Mittel — wenig Körper in Seitwärts | Mittel | Gute Datenbasis | ⚠️ Mittel |
| **ARB** | Mittel — junge Datenbasis | Noch aufbauend | Kurze Datenbasis (ab 2023) | ⚠️ Mittel |
| **DOT** | Mittel — oft indifferente Kerzen | Gering | Ausreichend | ⚠️ Mittel |
| **LINK** | Mittel — explosiv in Bull, träge sonst | Ungleichmäßig | Ausreichend | ⚠️ Mittel |
| **DOGE** | Schlecht — sentiment-getriebene Muster | Niedrig, nicht statistisch | Vorhanden aber unbrauchbar | ❌ Schlecht |
| **SHIB/PEPE** | Nicht lernbar — Pump-Candles | Keine Wiederholbarkeit | Zu kurze Datenbasis | ❌❌ Nicht geeignet |

### Empfohlene Kombinationen (Ranking)

| Rang | Kombination | Begründung |
|---|---|---|
| 🥇 1 | **BTC 4h / 6h** | Beste institutionelle Kerzenmuster, längste Datenbasis für DB |
| 🥇 1 | **ETH 4h / 6h** | Ähnlich BTC, exzellente Sequenz-Qualität |
| 🥈 2 | **BTC 2h / ETH 2h** | Mehr Sequenzen für schnelleres DB-Befüllen |
| 🥉 3 | **SOL 4h** | Klare Directional-Candles, gute Sequenzabdeckung |
| 4 | **BNB 4h** | Stabile, wiederholende Muster |
| 4 | **XRP 4h** | Gute Sequenzen in Range- und Trendphasen |
| 4 | **LTC 4h** | BTC-Muster, gute Datenbasis |
| 5 | **AVAX 4h** | Gute Bullmarkt-Sequenzen |
| ❌ | **Alles auf 15m / 30m** | Sequenzen zu kurz, kein statistischer Wert |
| ❌ | **DOGE / SHIB** | Muster nicht wiederholbar, kein Lerneffekt |

> **Hinweis:** Das Self-Learning greift nach jedem Trade. Je mehr Trades auf einem Coin/TF-Paar, desto besser wird die Quantum-State-DB. BTC 4h liefert die schnellste und zuverlässigste DB-Reife.

---

## Abhängigkeiten

```
ccxt>=4.2.0      # Exchange-Verbindung (Bitget)
pandas>=2.0.0    # Datenverarbeitung
numpy>=1.24.0    # Array-Operationen
ta>=0.10.2       # ATR-Berechnung
requests>=2.31.0 # Telegram
optuna           # Bayesianische Hyperparameter-Optimierung
sqlite3          # Built-in Python — keine Installation nötig
```

---

*Weiterentwicklung von [dnabot](https://github.com/Youra82/dnabot) — gleiche Architektur, reicheres physics-informiertes State-Encoding.*
