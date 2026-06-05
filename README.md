# ZeroBot — Renko Quant-Trading Bot

Ein quantitativer Crypto-Trading-Bot auf Basis von Renko-Charts.
Keine willkürlichen Signale — alle Parameter werden via Optuna statistisch optimiert.

> **Disclaimer:** Diese Software ist experimentell und dient ausschließlich Forschungszwecken.
> Der Handel mit Kryptowährungen birgt erhebliche finanzielle Risiken. Nutzung auf eigene Gefahr.

---

## Grundidee

Klassische Candlestick-Charts rauschen durch Zeit-Noise. **Renko-Charts filtern Zeit komplett heraus** — eine neue Brick entsteht erst wenn der Kurs sich um einen definierten Betrag bewegt:

```
Normale 4h-Kerze:  Kurs steigt 0.1% → Kerze gezeichnet  (viel Rauschen)
Renko-Brick:       Brick erst wenn Kurs > ATR × Multiplier steigt  (nur Bewegung zählt)
```

Der Optimizer findet pro Symbol/Timeframe die besten Werte für:

```
atr_multiplier       — Brick-Größe: ATR × X  (0.5–3.0)
trend_min_bricks     — Mindest-Bricks für Trend-Bestätigung  (2–6)
reversal_bricks      — Bricks für Reversal-Signal  (1–3)
vol_filter_enabled   — Volumen-Bestätigung ja/nein

atr_multiplier_sl    — SL-Abstand vom Entry in ATR-Vielfachen  (1.5–5.0)
risk_reward_ratio    — TP = SL × RRR  (1.5–5.0)
leverage             — Hebel  (5–20×)
trailing_stop_activation_rr    — Ab welchem R wird Trailing Stop aktiviert  (1.0–3.0)
trailing_stop_callback_rate_pct — Trailing Stop Callback in %  (0.2–2.0)
```

---

## Architektur

```
zerobot/
├── master_runner.py               # Cronjob-Orchestrator für Live-Trading
├── run_pipeline.sh                # Optimizer (Optuna findet beste Parameter)
├── show_results.sh                # Ergebnisse, Backtests, Portfolio-Simulation
├── run_analysis.sh                # Renko-spezifische Analysen & Sweeps
├── auto_optimizer_scheduler.py    # Automatischer Wochentimer: Neu-Optimierung
├── run_portfolio_optimizer.py     # Automatische Portfolio-Optimierung
├── install.sh                     # Erstinstallation auf VPS
├── update.sh                      # Git-Update (sichert secret.json)
├── run_tests.sh                   # Pytest-Sicherheitscheck
├── settings.json                  # Konfiguration (in Git)
├── secret.json                    # API-Keys (NICHT in Git)
│
└── src/zerobot/
    ├── strategy/
    │   ├── renko_engine.py        # Renko-Brick-Berechnung aus OHLCV
    │   ├── renko_logic.py         # Signal-Erkennung auf Brick-Sequenzen
    │   ├── run.py                 # Entry Point für eine Strategie
    │   └── configs/               # Optimierte Configs pro Symbol/TF (in Git)
    │
    ├── analysis/
    │   ├── optimizer.py           # Optuna Parameter-Suche
    │   ├── backtester.py          # Historische Simulation
    │   ├── portfolio_simulator.py # Portfolio-Simulation (gemeinsamer Kapital-Pool)
    │   ├── portfolio_optimizer.py # Beste Strategie-Kombination finden
    │   └── show_results.py        # Tabellen-Output (Einzel + Portfolio)
    │
    └── utils/
        ├── exchange.py            # Bitget CCXT Wrapper
        ├── trade_manager.py       # Entry/TP/SL + Trailing Stop
        ├── telegram.py            # Telegram-Benachrichtigungen
        ├── guardian.py            # Crash-Schutz Decorator
        └── timeframe_utils.py     # HTF-Ableitung
```

---

## Wie das System funktioniert

### Phase 1 — Optimizer (`run_pipeline.sh`)

```
Historische OHLCV-Daten (Bitget via CCXT)
    ↓
ATR berechnen → Renko-Bricks konstruieren
    ↓
Optuna: 200+ Trials — sucht beste Parameter-Kombination
    ↓
Constraints: MaxDD ≤ Limit | WinRate ≥ Minimum | Trades ≥ 15
    ↓
Beste Config gespeichert: src/zerobot/strategy/configs/config_SYMBOL_TF.json
```

> Der Optimizer vergleicht jede neue Config mit der bestehenden.
> Nur wenn das neue Ergebnis besser ist, wird die Config überschrieben.

### Phase 2 — Live-Trading (`master_runner.py`)

```
Jeder Cronjob-Lauf:
  1. Aktuelle OHLCV-Kerzen laden
  2. Renko-Bricks aus letzten N Kerzen berechnen
  3. Signal prüfen: Trend-Bricks + Reversal → Long oder Short?
  4. Volumen-Filter: Handelsvolumen > min_vol_ratio × MA? (falls aktiviert)
  5. Entry: Trigger-Limit-Order (0.05% Delta)
  6. SL: ATR × atr_multiplier_sl vom Entry-Preis
  7. TP: SL × risk_reward_ratio
  8. Trailing Stop: aktiviert bei trailing_stop_activation_rr × R
```

### Beispiel-Signal

```
[ZeroBot Signal]
  Symbol:    SOL/USDT:USDT (6h)
  Richtung:  LONG
  Renko ATR: 1.12 (Brick-Größe = 1.12 × ATR)
  Entry:     ~148.20 USDT (Trigger-Limit)
  SL:         144.80 USDT (ATR × 2.3 unter Entry)
  TP:         155.00 USDT (SL × 2.1 RRR)
  Trailing:   aktiviert ab 1.5×R, Callback 0.8%
  Hebel:      12×
```

---

## Konfiguration (`settings.json`)

```json
{
    "live_trading_settings": {
        "max_open_positions": 7,
        "use_auto_optimizer_results": false,
        "active_strategies": [
            { "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": true },
            { "symbol": "SOL/USDT:USDT", "timeframe": "6h", "active": true },
            { "symbol": "ETH/USDT:USDT", "timeframe": "4h", "active": false }
        ]
    },
    "optimization_settings": {
        "enabled": true,
        "schedule": {
            "day_of_week": 6,
            "hour": 15,
            "minute": 0,
            "interval": { "value": 7, "unit": "days" }
        },
        "start_capital": 100,
        "start_date": "2024-01-01",
        "end_date": "auto",
        "constraints": { "max_drawdown_pct": 30 },
        "send_telegram_on_completion": true
    }
}
```

| Parameter | Erklärung |
|---|---|
| `max_open_positions` | Maximale gleichzeitig offene Positionen |
| `active_strategies` | Welche Pairs live gehandelt werden (`active: true`) |
| `optimization_settings.enabled` | Automatische wöchentliche Neu-Optimierung ein/aus |
| `optimization_settings.schedule` | Wochentag (0=Mo, 6=So) + Uhrzeit |
| `optimization_settings.start_capital` | Startkapital für den Optimizer |
| `optimization_settings.constraints.max_drawdown_pct` | Maximaler erlaubter Drawdown |

---

## Installation

#### 1. Projekt klonen

```bash
git clone https://github.com/Youra82/zerobot.git
cd zerobot
```

#### 2. Installations-Skript ausführen

```bash
chmod +x install.sh
bash ./install.sh
```

Erstellt die virtuelle Python-Umgebung, installiert alle Abhängigkeiten und legt die Verzeichnisstruktur an.

#### 3. API-Keys eintragen

```bash
nano secret.json
```

```json
{
    "zerobot": [
        {
            "name": "Account1",
            "apiKey": "DEIN_API_KEY",
            "secret": "DEIN_API_SECRET",
            "password": "DEIN_API_PASSWORT"
        }
    ],
    "telegram": {
        "bot_token": "DEIN_BOT_TOKEN",
        "chat_id": "DEINE_CHAT_ID"
    }
}
```

---

## Workflow

#### 1. Coins und Timeframes konfigurieren

```bash
nano settings.json
```

`active_strategies` befüllen — `active: false` reicht zunächst (der Optimizer läuft für alle eingetragenen Pairs).

#### 2. Optimizer ausführen (Pipeline)

```bash
./run_pipeline.sh
```

Der Optimizer sucht via Optuna die besten Parameter pro Symbol/Timeframe. Interaktiv:
- Coins/Timeframes (leer = auto aus settings.json)
- Zeitraum (leer = automatisch nach Timeframe: 4h → 730 Tage, 6h → 1095 Tage)
- Startkapital, Trials, CPU-Kerne
- Optimierungsmodus (Strict: WR+DD+PnL | Best-Profit: nur DD)
- Optional: einzelne Parameter fix setzen statt Optuna frei lassen

Ergebnis: `src/zerobot/strategy/configs/config_SYMBOL_TF.json` pro Pair.

#### 3. Ergebnisse analysieren

```bash
./show_results.sh
```

| Modus | Funktion |
|---|---|
| **1) Einzel-Backtest** | Simuliert jede Config einzeln — zeigt Trades, WinRate, PnL, MaxDD, Hebel, SL ATR, RRR, Trailing, Renko ATR |
| **2) Manuelle Portfolio-Simulation** | Eigene Pair-Auswahl, kombiniertes Kapital, Kompoundierung |
| **3) Automatische Portfolio-Opt.** | Bot wählt das Portfolio mit maximalem PnL bei gegebenem MaxDD-Limit |

#### 4. Strategien live schalten

```bash
nano settings.json
```

```json
{ "symbol": "SOL/USDT:USDT", "timeframe": "6h", "active": true }
```

#### 5. Cronjob einrichten

```bash
crontab -e
```

```
*/15 * * * * cd /root/zerobot && .venv/bin/python3 master_runner.py >> logs/cron.log 2>&1
```

> Der `master_runner.py` ruft beim Start automatisch den `auto_optimizer_scheduler.py` auf.
> Dieser prüft ob eine Neu-Optimierung fällig ist und führt sie dann automatisch aus.
> Ein separater Cronjob für wöchentliches Re-Optimieren ist **nicht nötig**.

---

## Analysen (`run_analysis.sh`)

```bash
./run_analysis.sh
```

```
=======================================================
  ZeroBot — Renko Analysen
=======================================================

  ── Strategie-Analyse ───────────────────────────────
   1) Einzel-Backtest            (alle Configs einzeln)
   2) Manuelle Portfolio-Sim.    (eigene Auswahl)
   3) Auto Portfolio-Optimierung (bestes Portfolio finden)

  ── Renko-spezifische Analysen ──────────────────────
   4) Brick-Größen-Sweep         (ATR-Multiplier 0.5–2.5 testen)
   5) Trend-Längen-Analyse       (trend_min_bricks 2–6 vergleichen)
   6) Reversal-Bricks-Analyse    (reversal_bricks 1–4 vergleichen)
   7) Volumen-Filter Auswirkung  (mit / ohne vol_filter)

  ── Risiko & Robustheit ─────────────────────────────
   8) RR-Ratio Sweep             (1.0–4.0 vergleichen)
   9) ATR-SL-Multiplier Sweep    (1.0–4.0 vergleichen)
  10) Timeframe-Vergleich        (1h vs 4h vs 6h)

   0) Alle Analysen nacheinander (Standard-Werte)
```

| Analyse | Was man lernt |
|---|---|
| **4) Brick-Größen-Sweep** | Kleine Bricks = mehr Trades, mehr Rauschen. Große Bricks = weniger Trades, sauberere Signale. Zeigt das optimale ATR-Vielfache für diesen Coin. |
| **5) Trend-Längen-Analyse** | Wie viele Trend-Bricks vor dem Einstieg nötig sind. 2 = früher rein, riskanter. 6 = späte Bestätigung, weniger Fehlsignale. |
| **6) Reversal-Bricks** | 1 Brick Reversal = sofortiger Einstieg. 3 Bricks = warten auf stärkere Umkehr. Trade-off: Einstieg vs. Sicherheit. |
| **7) Volumen-Filter** | Ob der Volumen-Filter die Win-Rate verbessert oder zu viele gute Trades filtert. |
| **8) RR-Ratio Sweep** | Zu hoch = Trailing Stop wird selten aktiviert, viele SL-Hits. Zu niedrig = kleiner Gewinn pro Win. |
| **9) ATR-SL-Sweep** | Zu eng = vorzeitiger SL. Zu weit = großer Verlust pro Trade. |
| **10) Timeframe-Vergleich** | Welcher Timeframe für diesen Coin die meisten statistisch stabilen Renko-Muster liefert. |

---

## Automatische Wochentimer-Optimierung

Der `auto_optimizer_scheduler.py` läuft non-blocking bei jedem `master_runner.py`-Aufruf:

```
master_runner.py startet
    ↓
auto_optimizer_scheduler.py prüft: Ist Optimierung fällig?
    ├── Nein → sofort beendet (kein Overhead)
    └── Ja →
           optimizer.py              (neue Parameter via Optuna suchen)
               ↓
           run_portfolio_optimizer.py --auto-write
               (bestes Portfolio → settings.json aktualisieren)
               ↓
           Telegram: Start + Ende Benachrichtigung
```

Manuell erzwingen:

```bash
.venv/bin/python3 auto_optimizer_scheduler.py --force
```

---

## Tägliche Verwaltung & Wichtige Befehle

#### Logs ansehen

```bash
# Live mitverfolgen
tail -f logs/cron.log

# Nach Fehlern suchen
grep -i "ERROR" logs/cron.log

# Letzte 200 Zeilen
tail -n 200 logs/cron.log
```

#### Manueller Start (Test)

```bash
cd ~/zerobot && .venv/bin/python3 master_runner.py
```

#### Tests ausführen (vor dem ersten Live-Betrieb)

```bash
./run_tests.sh
```

#### Bot aktualisieren

```bash
./update.sh
```

Sichert automatisch `secret.json` vor dem `git reset --hard`.

#### Auto-Optimizer manuell auslösen

```bash
.venv/bin/python3 auto_optimizer_scheduler.py --force
```

#### Optimierungen zurücksetzen

Alle generierten Configs, die Optuna-Datenbank und den letzten Run löschen:

```bash
rm ~/zerobot/src/zerobot/strategy/configs/config_*.json
rm ~/zerobot/artifacts/db/optuna_studies_zerobot.db
rm ~/zerobot/artifacts/results/last_optimizer_run.json
```

Alles auf einmal:

```bash
rm ~/zerobot/src/zerobot/strategy/configs/config_*.json \
   ~/zerobot/artifacts/db/optuna_studies_zerobot.db \
   ~/zerobot/artifacts/results/last_optimizer_run.json
```

Danach `./run_pipeline.sh` für einen kompletten Neustart ohne Vorwissen.

---

## Coin & Timeframe Empfehlungen

ZeroBot ist eine **Renko-Strategie** — er baut Bricks aus dem ATR und sucht Trend- und Reversal-Muster. Benötigt: Coins mit klarer Richtungsbewegung und ausreichend historische Daten für den Optimizer.

### Effektive Renko-Sequenz-Dauer

| TF | 3 Trend-Bricks | 6 Trend-Bricks | Signal-Qualität | Geeignet |
|---|---|---|---|---|
| 15m | ~45min | ~1.5h | Noise-dominiert | ❌ |
| 1h | ~3h | ~6h | Marginal | ⚠️ |
| **2h** | **~6h** | **~12h** | **Intraday-Swing** | **✅** |
| **4h** | **~12h** | **~24h** | **Voller Handelstag** | **✅✅** |
| **6h** | **~18h** | **~36h** | **1–2 Tage Swing** | **✅✅** |
| 1d | ~3d | ~6d | Wochen-Trend | ✅ |

### Coin-Eignung

| Coin | Trend-Stärke | Renko-Qualität | Bewertung |
|---|---|---|---|
| **BTC** | Sehr hoch — klare institutionelle Trends | Exzellente Brick-Sequenzen | ✅✅ Beste Wahl |
| **ETH** | Sehr hoch — korreliert mit BTC | Sehr gute Sequenzen | ✅✅ Sehr gut |
| **SOL** | Hoch — starke Richtungskerzen | Gute Brick-Muster | ✅ Gut |
| **BNB** | Mittel-hoch — stabil | Gute Sequenzen | ✅ Gut |
| **XRP** | Mittel — range-lastig | Gute Range-Reversal | ✅ Gut |
| **AVAX** | Mittel-hoch im Bullmarkt | Ausreichend | ✅ Gut |
| **DOGE** | Sentiment-getrieben | Unzuverlässige Brick-Muster | ⚠️ Vorsicht |
| **SHIB/PEPE** | Pump-driven | Keine stabilen Sequenzen | ❌ Nicht geeignet |

### Empfohlene Kombinationen

| Rang | Kombination | Begründung |
|---|---|---|
| 🥇 1 | **BTC 4h + SOL 6h** | Beste Trend-Qualität, gute Unkorrelation |
| 🥇 1 | **BTC 6h + ETH 4h** | Komplementäre Timeframes, klare Trends |
| 🥈 2 | **BTC 4h + ETH 4h + SOL 4h** | Diversifikation, alle mit guter Renko-Qualität |
| 🥉 3 | **BNB 4h + XRP 4h** | Range-lastige Pairs → gut für Reversal-Strategie |
| ❌ | **Alles auf 15m/1h** | Bricks zu klein, Rauschen dominiert |

---

## Wichtige Regeln

- `secret.json` ist **nicht in Git** — wird von `update.sh` gesichert
- `artifacts/db/` ist **nicht in Git** — Optuna-Datenbank bleibt nach Updates erhalten
- `src/zerobot/strategy/configs/` ist **in Git** — Configs werden mit gepusht
- Immer erst `./run_pipeline.sh` bevor Live-Trading aktiviert wird
- Optimizer überschreibt eine Config nur wenn das neue Ergebnis besser ist

---

## Abhängigkeiten

```
ccxt         # Exchange-Verbindung (Bitget)
pandas       # Datenverarbeitung
numpy        # Array-Operationen
ta           # ATR-Berechnung für Renko-Bricks
optuna       # Bayesian Parameter-Optimierung
tqdm         # Fortschrittsbalken
requests     # Telegram
plotly       # Charts (optional, run_analysis.sh)
openpyxl     # Excel-Export (optional)
```
