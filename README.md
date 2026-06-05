# ZeroBot — Renko Quant-Trading Bot

Physics-informed Crypto Trading Bot: Hurst Exponent + Approximate Entropy + Transfer Entropy → Quantum State Pattern DB

## Architektur

- **Signal:** Renko-Bricks (ATR-basiert) + Trend-Erkennung + Volumen-Filter
- **Risiko:** ATR-basierter SL, RRR-basierter TP, Trailing Stop — alles optimiert via Optuna
- **Portfolio:** Kelly + Probabilistic Regime + Portfolio Risk Manager
- **Exchange:** Bitget Futures (CCXT), Isolated Margin

---

## Installation

```bash
git clone https://github.com/Youra82/zerobot.git
cd zerobot
./install.sh
```

Dann API-Keys eintragen:

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

## Konfiguration

`settings.json` — Symbole und Timeframes festlegen:

```json
{
    "live_trading_settings": {
        "active_strategies": [
            { "symbol": "BTC/USDT:USDT", "timeframe": "4h", "active": true },
            { "symbol": "SOL/USDT:USDT", "timeframe": "6h", "active": true }
        ]
    }
}
```

---

## Nutzung

### 1. Optimizer ausführen (Renko-Parameter + Risiko-Parameter optimieren)

```bash
./run_pipeline.sh
```

Der Optimizer findet pro Symbol/Timeframe die besten Werte für:
- `atr_multiplier` — Renko Brick-Größe (0.5–3.0 × ATR)
- `atr_multiplier_sl` — Stop-Loss Abstand (1.5–5.0 × ATR)
- `risk_reward_ratio` — Take-Profit = SL × RRR (1.5–5.0)
- `leverage` — Hebel (5–20×)
- `trailing_stop_activation_rr` + `trailing_stop_callback_rate_pct`

### 2. Ergebnisse anzeigen

```bash
./show_results.sh
```

### 3. Live-Trading starten

```bash
.venv/bin/python3 master_runner.py
```

### 4. Cronjob (alle 15 Min automatisch)

```bash
crontab -e
```

```
*/15 * * * * cd /root/zerobot && .venv/bin/python3 master_runner.py >> logs/cron.log 2>&1
```

---

## Update

```bash
./update.sh
```

---

## Optimierungen zurücksetzen

Alle generierten Configs, die Optuna-Datenbank und den letzten Optimizer-Run löschen:

```bash
rm ~/zerobot/src/zerobot/strategy/configs/config_*.json
rm ~/zerobot/artifacts/db/optuna_studies_zerobot.db
rm ~/zerobot/artifacts/results/last_optimizer_run.json
```

Oder alles auf einmal:

```bash
rm ~/zerobot/src/zerobot/strategy/configs/config_*.json \
   ~/zerobot/artifacts/db/optuna_studies_zerobot.db \
   ~/zerobot/artifacts/results/last_optimizer_run.json
```

Danach `./run_pipeline.sh` für einen kompletten Neustart.
