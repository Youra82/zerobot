#!/bin/bash
# run_pipeline.sh — Interaktive zerobot Pipeline
#
# Schritt 1: Optionen abfragen
# Schritt 2: scan_and_learn.py  → Quantum Discovery + Evolver
# Schritt 3: run_backtest.py    → Validierung der aktiven States
# Schritt 4: Zusammenfassung anzeigen

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
VENV_PATH="$SCRIPT_DIR/.venv/bin/activate"

# ── Venv prüfen ─────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst installieren:${NC}"
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi
source "$VENV_PATH"
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert.${NC}"

# ── Header ───────────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "       zerobot — Quantum State System"
echo "======================================================="
echo ""

# ── 1. Alte DB löschen? ──────────────────────────────────────────────────────
DB_PATH="$SCRIPT_DIR/artifacts/db/quantum.db"
if [ -f "$DB_PATH" ]; then
    read -p "Alte Quantum-Datenbank vor dem Start löschen (Neustart)? (j/n) [Standard: n]: " RESET_DB
    RESET_DB="${RESET_DB//[$'\r\n ']/}"
    if [[ "$RESET_DB" == "j" || "$RESET_DB" == "J" || "$RESET_DB" == "y" || "$RESET_DB" == "Y" ]]; then
        rm -f "$DB_PATH"
        echo -e "${GREEN}✔ Alte Quantum-DB gelöscht — Neustart.${NC}"
    else
        echo -e "${GREEN}✔ Bestehende Quantum-DB wird beibehalten.${NC}"
    fi
else
    echo -e "${CYAN}ℹ  Keine bestehende Quantum-DB gefunden — wird neu erstellt.${NC}"
fi

# ── 2. Coins / Timeframes ────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Coins und Timeframes:${NC}"
echo "  Leer lassen → automatisch aus active_strategies in settings.json übernehmen"
echo ""
read -p "Coin(s) eingeben (z.B. BTC ETH SOL) [leer=auto]: " COINS_INPUT
read -p "Timeframe(s) eingeben (z.B. 4h 1h) [leer=auto]: " TF_INPUT

COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
TF_INPUT="${TF_INPUT//[$'\r\n']/}"

if [ -n "$COINS_INPUT" ] && [ -n "$TF_INPUT" ]; then
    echo -e "${CYAN}ℹ  Explizite Auswahl: Coins=$COINS_INPUT | Timeframes=$TF_INPUT${NC}"
    export ZEROBOT_OVERRIDE_COINS="$COINS_INPUT"
    export ZEROBOT_OVERRIDE_TFS="$TF_INPUT"
elif [ -n "$COINS_INPUT" ]; then
    export ZEROBOT_OVERRIDE_COINS="$COINS_INPUT"
    echo -e "${CYAN}ℹ  Coins: $COINS_INPUT | Timeframes: aus active_strategies${NC}"
elif [ -n "$TF_INPUT" ]; then
    export ZEROBOT_OVERRIDE_TFS="$TF_INPUT"
    echo -e "${CYAN}ℹ  Coins: aus active_strategies | Timeframes: $TF_INPUT${NC}"
else
    echo -e "${GREEN}✔ Coins und Timeframes werden aus active_strategies übernommen.${NC}"
fi

# ── 3. History-Tage ──────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}--- Empfehlung: Optimaler Rückblick-Zeitraum ---${NC}"
printf "  %-12s  %s\n" "Zeitfenster" "Empfohlener Rückblick (Tage)"
printf "  %-12s  %s\n" "──────────" "──────────────────────────"
printf "  %-12s  %s\n" "5m, 15m"    "60 - 180 Tage"
printf "  %-12s  %s\n" "30m, 1h"    "180 - 365 Tage"
printf "  %-12s  %s\n" "2h, 4h"     "365 - 730 Tage"
printf "  %-12s  %s\n" "6h, 1d"     "730 - 1095 Tage"
echo ""
read -p "History-Tage (oder 'a' für Automatik nach Timeframe) [Standard: a]: " HISTORY_INPUT
HISTORY_INPUT="${HISTORY_INPUT//[$'\r\n ']/}"

HISTORY_ARG=""
if [[ "$HISTORY_INPUT" =~ ^[0-9]+$ ]]; then
    HISTORY_ARG="--history-days $HISTORY_INPUT"
    echo -e "${CYAN}ℹ  Fester Rückblick: ${HISTORY_INPUT} Tage${NC}"
else
    echo -e "${GREEN}✔ Automatischer Rückblick nach Timeframe.${NC}"
fi

# ── 4. Backtest nach Discovery? ───────────────────────────────────────────────
echo ""
read -p "Backtest nach Discovery durchführen? (j/n) [Standard: j]: " RUN_BT
RUN_BT="${RUN_BT//[$'\r\n ']/}"
RUN_BT="${RUN_BT:-j}"

CAPITAL=50
RISK=1.0
BT_START_DATE_ARG=""
BT_END_DATE_ARG=""
if [[ "$RUN_BT" == "j" || "$RUN_BT" == "J" || "$RUN_BT" == "y" || "$RUN_BT" == "Y" ]]; then
    read -p "Startkapital in USDT [Standard: 50]: " CAP_INPUT
    CAP_INPUT="${CAP_INPUT//[$'\r\n ']/}"
    if [[ "$CAP_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=$CAP_INPUT; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK_INPUT
    RISK_INPUT="${RISK_INPUT//[$'\r\n ']/}"
    if [[ "$RISK_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=$RISK_INPUT; fi

    echo ""
    echo -e "${YELLOW}--- Train/Test Split ---${NC}"
    echo "  70/30: Discovery auf 70% der Daten, Backtest auf letzten 30%"
    echo "  Nein:  Backtest auf denselben Daten (In-Sample, optimistischer)"
    echo ""
    read -p "70/30 Out-of-Sample Split verwenden? (j/n) [Standard: j]: " USE_SPLIT
    USE_SPLIT="${USE_SPLIT//[$'\r\n ']/}"
    USE_SPLIT="${USE_SPLIT:-j}"

    if [[ "$USE_SPLIT" == "j" || "$USE_SPLIT" == "J" || "$USE_SPLIT" == "y" || "$USE_SPLIT" == "Y" ]]; then
        TOTAL_DAYS=730
        if [[ "$HISTORY_INPUT" =~ ^[0-9]+$ ]]; then
            TOTAL_DAYS=$HISTORY_INPUT
        fi
        TRAIN_DAYS=$(( TOTAL_DAYS * 70 / 100 ))
        TEST_DAYS=$(( TOTAL_DAYS - TRAIN_DAYS ))
        SPLIT_DATE=$(date -d "$TEST_DAYS days ago" +%F)
        TODAY=$(date +%F)
        BT_START_DATE_ARG="--start-date $SPLIT_DATE"
        BT_END_DATE_ARG="--end-date $TODAY"
        echo -e "${CYAN}ℹ  Training:  letzte ${TRAIN_DAYS} Tage (bis $SPLIT_DATE)${NC}"
        echo -e "${CYAN}ℹ  Backtest:  letzte ${TEST_DAYS} Tage ($SPLIT_DATE → $TODAY)${NC}"
        HISTORY_ARG="--history-days $TRAIN_DAYS"
    fi
fi

# ── Pipeline starten ─────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "  Pipeline startet..."
echo "======================================================="
echo ""

# Pair-Liste aufbauen (via Python-Helfer)
PAIRS=""
if [ -n "${ZEROBOT_OVERRIDE_COINS:-}" ] || [ -n "${ZEROBOT_OVERRIDE_TFS:-}" ]; then
    PAIRS=$($PYTHON - <<'PYEOF'
import os, json

coins_raw = os.environ.get('ZEROBOT_OVERRIDE_COINS', '').strip()
tfs_raw   = os.environ.get('ZEROBOT_OVERRIDE_TFS', '').strip()

try:
    with open('settings.json') as f:
        s = json.load(f)
    active = s.get('live_trading_settings', {}).get('active_strategies', [])
    auto_coins = list(dict.fromkeys(x['symbol'] for x in active if x.get('symbol')))
    auto_tfs   = list(dict.fromkeys(x['timeframe'] for x in active if x.get('timeframe')))
except Exception:
    auto_coins = ['BTC/USDT:USDT']
    auto_tfs   = ['4h']

def to_symbol(coin):
    coin = coin.strip().upper()
    if '/' not in coin:
        return f"{coin}/USDT:USDT"
    return coin

coins = [to_symbol(c) for c in coins_raw.split()] if coins_raw else auto_coins
tfs   = [t.strip() for t in tfs_raw.split()] if tfs_raw else auto_tfs

for sym in coins:
    for tf in tfs:
        print(f"{sym} {tf}")
PYEOF
    )
fi

if [ -n "$PAIRS" ]; then
    echo -e "${CYAN}Scan-Paare:${NC}"
    echo "$PAIRS" | while read -r sym tf; do
        echo "  → $sym ($tf)"
    done
    echo ""

    echo -e "${YELLOW}[Schritt 1/3] Quantum Discovery + Evolver...${NC}"
    echo "$PAIRS" | while IFS=' ' read -r sym tf; do
        echo ""
        echo -e "${CYAN}  Scanne: $sym ($tf)${NC}"
        $PYTHON "$SCRIPT_DIR/scan_and_learn.py" \
            --symbol "$sym" --timeframe "$tf" $HISTORY_ARG --no-evolve
    done
    echo ""
    echo -e "${CYAN}  Evolver läuft...${NC}"
    echo "$PAIRS" | while IFS=' ' read -r sym tf; do
        $PYTHON "$SCRIPT_DIR/scan_and_learn.py" \
            --symbol "$sym" --timeframe "$tf" $HISTORY_ARG
    done
else
    echo -e "${YELLOW}[Schritt 1/3] Quantum Discovery + Evolver...${NC}"
    $PYTHON "$SCRIPT_DIR/scan_and_learn.py" $HISTORY_ARG
fi

echo ""

# Schritt 2: Backtest
if [[ "$RUN_BT" == "j" || "$RUN_BT" == "J" || "$RUN_BT" == "y" || "$RUN_BT" == "Y" ]]; then
    echo -e "${YELLOW}[Schritt 2/3] Backtest...${NC}"
    if [ -n "$PAIRS" ]; then
        echo "$PAIRS" | while IFS=' ' read -r sym tf; do
            echo -e "${CYAN}  Backtest: $sym ($tf)${NC}"
            $PYTHON "$SCRIPT_DIR/run_backtest.py" \
                --symbol "$sym" --timeframe "$tf" \
                --capital "$CAPITAL" --risk "$RISK" \
                $BT_START_DATE_ARG $BT_END_DATE_ARG
        done
    else
        $PYTHON "$SCRIPT_DIR/run_backtest.py" \
            --capital "$CAPITAL" --risk "$RISK" \
            $BT_START_DATE_ARG $BT_END_DATE_ARG
    fi
    echo ""
fi

# Schritt 3: Zusammenfassung
echo -e "${YELLOW}[Schritt 3/3] Quantum State Library...${NC}"
$PYTHON - <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath('.')), 'src'))
sys.path.insert(0, 'src')
from zerobot.physics.database import StateDB
from zerobot.physics.evolver import print_state_report
db = StateDB('artifacts/db/quantum.db')
print_state_report(db)
db.close()
PYEOF

echo ""
echo "======================================================="
echo -e "  ${GREEN}Pipeline abgeschlossen!${NC}"
echo ""
echo "  Nächste Schritte:"
echo "    1. Ergebnisse prüfen:     ./show_results.sh"
echo "    2. Strategie aktivieren:  settings.json → \"active\": true"
echo "    3. Cronjob einrichten:    crontab -e"
echo "======================================================="

deactivate
