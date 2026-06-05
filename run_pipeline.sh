#!/bin/bash
# run_pipeline.sh — ZeroBot Renko Optimierungs-Pipeline

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
VENV_PATH="$SCRIPT_DIR/.venv/bin/activate"

# ── Venv prüfen ──────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst ./install.sh ausführen!${NC}"
    exit 1
fi
source "$VENV_PATH"
echo -e "${GREEN}✔ Virtuelle Umgebung wurde erfolgreich aktiviert.${NC}"

# ── Pakete prüfen ─────────────────────────────────────────────────────────────
$PYTHON -c "import ta" 2>/dev/null || {
    echo -e "${YELLOW}⚠ Fehlende Pakete — installiere nach requirements.txt...${NC}"
    $PYTHON -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
    echo -e "${GREEN}✔ Pakete installiert.${NC}"
}

# ── Empfohlene Brick-Größen (ATR-Multiplier) pro Coin ────────────────────────
declare -A BRICK_DEFAULTS
BRICK_DEFAULTS["BTC"]="0.8"
BRICK_DEFAULTS["ETH"]="0.9"
BRICK_DEFAULTS["SOL"]="1.1"
BRICK_DEFAULTS["DOGE"]="1.2"
BRICK_DEFAULTS["XRP"]="1.1"
BRICK_DEFAULTS["ADA"]="1.1"
BRICK_DEFAULTS["BNB"]="0.9"
BRICK_DEFAULTS["MATIC"]="1.2"
BRICK_DEFAULTS["AVAX"]="1.0"
BRICK_DEFAULTS["LINK"]="1.0"
BRICK_DEFAULTS["DEFAULT"]="1.0"

echo ""
echo "======================================================="
echo "       ZeroBot Renko Optimierungs-Pipeline"
echo "======================================================="
echo ""

# ── Alte Configs löschen? ────────────────────────────────────────────────────
CONFIGS_DIR="$SCRIPT_DIR/src/zerobot/strategy/configs"
mkdir -p "$CONFIGS_DIR"
if ls "$CONFIGS_DIR"/config_*.json &>/dev/null 2>&1; then
    read -p "Möchtest du alle alten, generierten Configs vor dem Start löschen?
Dies wird für einen kompletten Neustart empfohlen. (j/n) [Standard: n]: " RESET_CONFIGS
    RESET_CONFIGS="${RESET_CONFIGS//[$'\r\n ']/}"
    if [[ "$RESET_CONFIGS" == "j" || "$RESET_CONFIGS" == "J" || "$RESET_CONFIGS" == "y" || "$RESET_CONFIGS" == "Y" ]]; then
        rm -f "$CONFIGS_DIR"/config_*.json
        echo -e "${GREEN}✔ Alte Configs gelöscht — Neustart.${NC}"
    else
        echo -e "${GREEN}✔ Alte Configs werden beibehalten.${NC}"
    fi
else
    echo -e "${CYAN}ℹ  Keine bestehenden Configs — werden neu erstellt.${NC}"
fi

# ── Coins und Timeframes ──────────────────────────────────────────────────────
echo ""
read -p "Handelspaar(e) eingeben (ohne /USDT, z.B. BTC ETH DOGE) [leer=auto aus settings.json]: " COINS_INPUT
read -p "Zeitfenster eingeben (z.B. 4h 1h 6h) [leer=auto aus settings.json]: " TF_INPUT
COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
TF_INPUT="${TF_INPUT//[$'\r\n']/}"

export ZB_COINS="$COINS_INPUT"
export ZB_TFS="$TF_INPUT"

PAIRS=$($PYTHON - <<'PYEOF'
import os, json
coins_raw = os.environ.get('ZB_COINS', '').strip()
tfs_raw   = os.environ.get('ZB_TFS',   '').strip()
try:
    with open('settings.json') as f:
        s = json.load(f)
    active     = s.get('live_trading_settings', {}).get('active_strategies', [])
    auto_coins = list(dict.fromkeys(x['symbol'] for x in active if x.get('symbol')))
    auto_tfs   = list(dict.fromkeys(x['timeframe'] for x in active if x.get('timeframe')))
except Exception:
    auto_coins = ['DOGE/USDT:USDT']
    auto_tfs   = ['4h']

def to_symbol(c):
    c = c.strip().upper()
    return c if '/' in c else f"{c}/USDT:USDT"

coins = [to_symbol(c) for c in coins_raw.split()] if coins_raw else auto_coins
tfs   = [t.strip() for t in tfs_raw.split()]       if tfs_raw   else auto_tfs
for sym in coins:
    for tf in tfs:
        print(f"{sym} {tf}")
PYEOF
)

# ── Lookback-Empfehlung ───────────────────────────────────────────────────────
echo ""
echo "--- Empfehlung: Optimaler Rückblick-Zeitraum ---"
printf "+-------------+--------------------------------+\n"
printf "| %-11s | %-30s |\n" "Zeitfenster" "Empfohlener Rückblick (Tage)"
printf "+-------------+--------------------------------+\n"
printf "| %-11s | %-30s |\n" "5m, 15m"  "60 - 180 Tage"
printf "| %-11s | %-30s |\n" "30m, 1h"  "180 - 365 Tage"
printf "| %-11s | %-30s |\n" "2h, 4h"   "550 - 730 Tage"
printf "| %-11s | %-30s |\n" "6h, 1d"   "1095 - 1825 Tage"
printf "+-------------+--------------------------------+\n"
echo ""

read -p "Startdatum (JJJJ-MM-TT) oder 'a' für Automatik [Standard: a]: " START_INPUT
START_INPUT="${START_INPUT//[$'\r\n ']/}"
read -p "Enddatum (JJJJ-MM-TT) [Standard: Heute]: " END_INPUT
END_INPUT="${END_INPUT//[$'\r\n ']/}"
END_INPUT="${END_INPUT:-$(date +%Y-%m-%d)}"

if [[ -z "$START_INPUT" || "$START_INPUT" == "a" ]]; then
    START_INPUT=$($PYTHON - <<PYEOF2
from datetime import datetime, timedelta
pairs = """$PAIRS"""
lookback_map = {'5m':120,'15m':120,'30m':365,'1h':365,'2h':730,'4h':730,'6h':1095,'1d':1825}
tfs = set()
for line in pairs.strip().split('\n'):
    parts = line.strip().split()
    if len(parts) == 2:
        tfs.add(parts[1])
days = max((lookback_map.get(tf, 730) for tf in tfs), default=730)
print((datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d'))
PYEOF2
    )
    while IFS=' ' read -r sym tf; do
        echo -e "${CYAN}INFO: Automatisches Startdatum für $tf ($(
            case $tf in 4h) echo "730 Tage";; 6h) echo "1095 Tage";; 1h) echo "365 Tage";; *) echo "auto";; esac
        ) Rückblick) gesetzt auf: $START_INPUT${NC}"
        break
    done <<< "$PAIRS"
fi

# ── Optimierungs-Parameter ────────────────────────────────────────────────────
echo ""
read -p "Startkapital in USDT [Standard: 100]: " CAPITAL_INPUT
CAPITAL_INPUT="${CAPITAL_INPUT//[$'\r\n ']/}"
if ! [[ "$CAPITAL_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL_INPUT=100; fi

read -p "CPU-Kerne [Standard: -1 für alle]: " JOBS_INPUT
JOBS_INPUT="${JOBS_INPUT//[$'\r\n ']/}"
if [[ "$JOBS_INPUT" == "-1" || -z "$JOBS_INPUT" ]]; then
    JOBS_INPUT=$(nproc 2>/dev/null || echo 1)
fi
if ! [[ "$JOBS_INPUT" =~ ^[0-9]+$ ]]; then JOBS_INPUT=1; fi

read -p "Anzahl Trials [Standard: 200]: " TRIALS_INPUT
TRIALS_INPUT="${TRIALS_INPUT//[$'\r\n ']/}"
if ! [[ "$TRIALS_INPUT" =~ ^[0-9]+$ ]]; then TRIALS_INPUT=200; fi

echo ""
echo "Wähle einen Optimierungs-Modus:"
echo "  1) Strenger Modus   (Profitabel + WR >= Min. Win-Rate + MaxDD <= Limit)"
echo "  2) Best-Profit-Modus (Nur MaxDD-Limit, maximiert PnL)"
read -p "Auswahl (1-2) [Standard: 1]: " MODE_INPUT
MODE_INPUT="${MODE_INPUT//[$'\r\n ']/}"
if [[ "$MODE_INPUT" == "2" ]]; then OPTIM_MODE="best_profit"; else OPTIM_MODE="strict"; fi

read -p "Max Drawdown % [Standard: 30]: " DD_INPUT
DD_INPUT="${DD_INPUT//[$'\r\n ']/}"
if ! [[ "$DD_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then DD_INPUT=30; fi

read -p "Min. Win-Rate % [Standard: 45]: " WR_INPUT
WR_INPUT="${WR_INPUT//[$'\r\n ']/}"
if ! [[ "$WR_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then WR_INPUT=45; fi

# ── Renko Strategie-Parameter (optional fixieren) ─────────────────────────────
echo ""
echo "--- Renko Strategie-Parameter ---"
echo ""
echo "  Was Optuna optimiert wenn du leer lässt:"
printf "  %-22s %s\n" "atr_multiplier"   "Brick-Größe = ATR × X          (0.5–3.0)"
printf "  %-22s %s\n" "trend_min_bricks" "Mindest-Bricks für Trend       (2–6)"
printf "  %-22s %s\n" "reversal_bricks"  "Bricks für Reversal-Signal     (1–3)"
printf "  %-22s %s\n" "vol_filter"       "Volumen-Bestätigung ja/nein    (auto)"
echo ""
echo "  Zahl/Wert eingeben → wird fixiert | leer → Optuna optimiert frei"
echo ""

# ATR-Multiplier pro Pair
declare -A PAIR_ATR_OVERRIDES
while IFS=' ' read -r sym tf; do
    COIN=$(echo "$sym" | cut -d'/' -f1)
    DEFAULT_ATR="${BRICK_DEFAULTS[$COIN]:-${BRICK_DEFAULTS[DEFAULT]}}"
    read -p "  ATR-Multiplier für $COIN/$tf [Empfehlung: $DEFAULT_ATR, leer=Optuna frei]: " ATR_INPUT
    ATR_INPUT="${ATR_INPUT//[$'\r\n ']/}"
    PAIR_KEY="${sym}_${tf}"
    if [[ "$ATR_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        PAIR_ATR_OVERRIDES["$PAIR_KEY"]="$ATR_INPUT"
        echo -e "    ${GREEN}→ Fixiert auf: $ATR_INPUT${NC}"
    else
        PAIR_ATR_OVERRIDES["$PAIR_KEY"]=""
        echo -e "    ${CYAN}→ Optuna optimiert frei${NC}"
    fi
done <<< "$PAIRS"

echo ""

# Trend-Min-Bricks (global für alle Pairs)
read -p "  trend_min_bricks — Mindest-Trend-Bricks [2-6, leer=Optuna frei]: " TREND_INPUT
TREND_INPUT="${TREND_INPUT//[$'\r\n ']/}"
TREND_ARG=""
if [[ "$TREND_INPUT" =~ ^[2-6]$ ]]; then
    TREND_ARG="$TREND_INPUT"
    echo -e "    ${GREEN}→ Fixiert auf: $TREND_INPUT${NC}"
else
    echo -e "    ${CYAN}→ Optuna optimiert frei${NC}"
fi

read -p "  reversal_bricks  — Reversal-Bestätigung [1-3, leer=Optuna frei]: " REVERSAL_INPUT
REVERSAL_INPUT="${REVERSAL_INPUT//[$'\r\n ']/}"
REVERSAL_ARG=""
if [[ "$REVERSAL_INPUT" =~ ^[1-3]$ ]]; then
    REVERSAL_ARG="$REVERSAL_INPUT"
    echo -e "    ${GREEN}→ Fixiert auf: $REVERSAL_INPUT${NC}"
else
    echo -e "    ${CYAN}→ Optuna optimiert frei${NC}"
fi

read -p "  vol_filter       — Volumen-Filter [j/n, leer=Optuna frei]: " VOL_INPUT
VOL_INPUT="${VOL_INPUT//[$'\r\n ']/}"
VOL_ARG=""
if [[ "$VOL_INPUT" == "j" || "$VOL_INPUT" == "J" || "$VOL_INPUT" == "y" || "$VOL_INPUT" == "Y" ]]; then
    VOL_ARG="true"
    echo -e "    ${GREEN}→ Fixiert auf: aktiviert${NC}"
elif [[ "$VOL_INPUT" == "n" || "$VOL_INPUT" == "N" ]]; then
    VOL_ARG="false"
    echo -e "    ${GREEN}→ Fixiert auf: deaktiviert${NC}"
else
    echo -e "    ${CYAN}→ Optuna optimiert frei${NC}"
fi

# ── Optimizer pro Pair ────────────────────────────────────────────────────────
OPTIMIZER="$SCRIPT_DIR/src/zerobot/analysis/optimizer.py"

while IFS=' ' read -r sym tf; do
    PAIR_KEY="${sym}_${tf}"
    ATR_OVERRIDE="${PAIR_ATR_OVERRIDES[$PAIR_KEY]}"
    COIN=$(echo "$sym" | cut -d'/' -f1)

    echo ""
    echo "======================================================="
    echo "  Bearbeite Pipeline für: $COIN ($tf)"
    echo "  Datenzeitraum: $START_INPUT bis $END_INPUT"
    echo "======================================================="
    echo ""

    # Baue optionale Args
    EXTRA_ARGS=""
    [ -n "$ATR_OVERRIDE" ]  && EXTRA_ARGS="$EXTRA_ARGS --fixed-atr-multiplier $ATR_OVERRIDE"
    [ -n "$TREND_ARG" ]     && EXTRA_ARGS="$EXTRA_ARGS --fixed-trend-min-bricks $TREND_ARG"
    [ -n "$REVERSAL_ARG" ]  && EXTRA_ARGS="$EXTRA_ARGS --fixed-reversal-bricks $REVERSAL_ARG"
    [ -n "$VOL_ARG" ]       && EXTRA_ARGS="$EXTRA_ARGS --fixed-vol-filter $VOL_ARG"

    $PYTHON "$OPTIMIZER" \
        --pairs         "${sym}|${tf}" \
        --start_date    "$START_INPUT" \
        --end_date      "$END_INPUT" \
        --trials        "$TRIALS_INPUT" \
        --jobs          "$JOBS_INPUT" \
        --max_drawdown  "$DD_INPUT" \
        --start_capital "$CAPITAL_INPUT" \
        --min_win_rate  "$WR_INPUT" \
        --min_pnl 0 \
        --mode "$OPTIM_MODE" \
        $EXTRA_ARGS

done <<< "$PAIRS"

# ── Portfolio-Optimierung ─────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "  Portfolio-Optimierung"
echo "======================================================="
echo ""

$PYTHON "$SCRIPT_DIR/run_portfolio_optimizer.py" \
    --capital    "$CAPITAL_INPUT" \
    --max-dd     "$DD_INPUT" \
    --start-date "$START_INPUT" \
    --end-date   "$END_INPUT"

# ── Ergebnisse ────────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo "  Ergebnisse"
echo "======================================================="
echo ""

$PYTHON "$SCRIPT_DIR/src/zerobot/analysis/show_results.py" --mode 1

echo ""
echo "======================================================="
echo -e "  ${GREEN}Pipeline abgeschlossen!${NC}"
echo ""
echo "  Nächste Schritte:"
echo "    1. Ergebnisse prüfen:    ./show_results.sh"
echo "    2. settings.json:        \"active\": true setzen"
echo "    3. Bot starten:          python3 master_runner.py"
echo "======================================================="

deactivate
