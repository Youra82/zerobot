#!/bin/bash
# run_pipeline.sh — ZeroBot Renko Interaktive Pipeline
#
# Schritt 1: Coins / Timeframes / Brick-Größen festlegen
# Schritt 2: Optuna-Optimierung pro Pair
# Schritt 3: Portfolio-Optimierung
# Schritt 4: Ergebnisse anzeigen

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

# ── Empfohlene Renko Brick-Größen (ATR-Multiplier) pro Coin ─────────────────
# Niedrige Preise (DOGE, XRP) → größerer Multiplier (mehr Rauschen rausfiltern)
# Große Coins (BTC, ETH) → kleinerer Multiplier (präzisere Bricks)
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

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

echo ""
echo "======================================================="
echo -e "  ${BOLD}ZeroBot — Renko Pipeline${NC}"
echo "  Interaktive Optimierung + Portfolio-Auswahl"
echo "======================================================="
echo ""

# ── 1. Coins und Timeframes ──────────────────────────────────────────────────
echo -e "${YELLOW}Coins und Timeframes:${NC}"
echo "  Leer lassen → automatisch aus active_strategies in settings.json"
echo ""
read -p "Coin(s) eingeben (z.B. BTC ETH DOGE) [leer=auto]: " COINS_INPUT
read -p "Timeframe(s) eingeben (z.B. 4h 1h) [leer=auto]: " TF_INPUT

COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
TF_INPUT="${TF_INPUT//[$'\r\n']/}"

# Paarliste aufbauen via Python
PAIRS=$($PYTHON - <<'PYEOF'
import os, sys, json

coins_raw = os.environ.get('ZB_COINS', '').strip()
tfs_raw   = os.environ.get('ZB_TFS',   '').strip()

try:
    with open('settings.json') as f:
        s = json.load(f)
    active    = s.get('live_trading_settings', {}).get('active_strategies', [])
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

export ZB_COINS="$COINS_INPUT"
export ZB_TFS="$TF_INPUT"

PAIRS=$($PYTHON - <<'PYEOF'
import os, json

coins_raw = os.environ.get('ZB_COINS', '').strip()
tfs_raw   = os.environ.get('ZB_TFS',   '').strip()

try:
    with open('settings.json') as f:
        s = json.load(f)
    active    = s.get('live_trading_settings', {}).get('active_strategies', [])
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

echo ""
echo -e "${CYAN}Zu optimierende Pairs:${NC}"
echo "$PAIRS" | while read -r sym tf; do
    echo "  → $sym ($tf)"
done
echo ""

# ── 2. Renko Brick-Größen pro Pair definieren ────────────────────────────────
echo -e "${YELLOW}Renko Brick-Größen (ATR-Multiplier):${NC}"
echo "  Empfehlungen:"
printf "  %-10s %s\n" "BTC/ETH"  "0.7–0.9  (geringe Volatilität, kleine Bricks)"
printf "  %-10s %s\n" "SOL/BNB"  "0.9–1.1  (mittlere Volatilität)"
printf "  %-10s %s\n" "DOGE/XRP" "1.0–1.4  (hohe Volatilität, größere Bricks)"
echo ""
echo "  Leer lassen → Optuna optimiert den Multiplier frei (0.5–3.0)"
echo "  Zahl eingeben → Multiplier wird fixiert (nicht von Optuna verändert)"
echo ""

declare -A PAIR_ATR_OVERRIDES

while IFS=' ' read -r sym tf; do
    COIN=$(echo "$sym" | cut -d'/' -f1)
    DEFAULT_ATR="${BRICK_DEFAULTS[$COIN]:-${BRICK_DEFAULTS[DEFAULT]}}"
    read -p "  ATR-Multiplier für $COIN/$tf [Standard: $DEFAULT_ATR, leer=Optuna frei]: " ATR_INPUT
    ATR_INPUT="${ATR_INPUT//[$'\r\n ']/}"
    PAIR_KEY="${sym}_${tf}"
    if [[ "$ATR_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        PAIR_ATR_OVERRIDES["$PAIR_KEY"]="$ATR_INPUT"
        echo -e "    ${GREEN}→ Fixiert: $ATR_INPUT${NC}"
    else
        PAIR_ATR_OVERRIDES["$PAIR_KEY"]=""
        echo -e "    ${CYAN}→ Optuna optimiert frei${NC}"
    fi
done <<< "$PAIRS"

# ── 3. Optimierungs-Parameter ─────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Optimierungs-Einstellungen:${NC}"
read -p "Anzahl Trials pro Pair [Standard: 200]: " TRIALS_INPUT
TRIALS_INPUT="${TRIALS_INPUT//[$'\r\n ']/}"
if ! [[ "$TRIALS_INPUT" =~ ^[0-9]+$ ]]; then TRIALS_INPUT=200; fi

read -p "Parallele Jobs [Standard: 4]: " JOBS_INPUT
JOBS_INPUT="${JOBS_INPUT//[$'\r\n ']/}"
if ! [[ "$JOBS_INPUT" =~ ^[0-9]+$ ]]; then JOBS_INPUT=4; fi

read -p "Startkapital in USDT [Standard: 100]: " CAPITAL_INPUT
CAPITAL_INPUT="${CAPITAL_INPUT//[$'\r\n ']/}"
if ! [[ "$CAPITAL_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL_INPUT=100; fi

read -p "Max. Drawdown in % [Standard: 30]: " DD_INPUT
DD_INPUT="${DD_INPUT//[$'\r\n ']/}"
if ! [[ "$DD_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then DD_INPUT=30; fi

read -p "Min. Win-Rate in % [Standard: 45]: " WR_INPUT
WR_INPUT="${WR_INPUT//[$'\r\n ']/}"
if ! [[ "$WR_INPUT" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then WR_INPUT=45; fi

read -p "Modus (strict/best_profit) [Standard: strict]: " MODE_INPUT
MODE_INPUT="${MODE_INPUT//[$'\r\n ']/}"
if [[ "$MODE_INPUT" != "best_profit" ]]; then MODE_INPUT="strict"; fi

# Zeitraum
DEFAULT_END=$(date +%Y-%m-%d)
DEFAULT_START="2024-01-01"
read -p "Startdatum [Standard: $DEFAULT_START]: " START_INPUT
START_INPUT="${START_INPUT//[$'\r\n ']/}"
START_INPUT="${START_INPUT:-$DEFAULT_START}"
read -p "Enddatum   [Standard: $DEFAULT_END]: " END_INPUT
END_INPUT="${END_INPUT//[$'\r\n ']/}"
END_INPUT="${END_INPUT:-$DEFAULT_END}"

# ── 4. Optimizer pro Pair ─────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo -e "  ${YELLOW}[Schritt 1/3] Renko Parameter-Optimierung${NC}"
echo "======================================================="

OPTIMIZER="$SCRIPT_DIR/src/zerobot/analysis/optimizer.py"

while IFS=' ' read -r sym tf; do
    PAIR_KEY="${sym}_${tf}"
    ATR_OVERRIDE="${PAIR_ATR_OVERRIDES[$PAIR_KEY]}"
    COIN=$(echo "$sym" | cut -d'/' -f1)

    echo ""
    echo -e "${CYAN}  Optimiere: $sym ($tf)${NC}"
    if [ -n "$ATR_OVERRIDE" ]; then
        echo -e "  ${CYAN}  Brick-Größe fixiert: ATR × $ATR_OVERRIDE${NC}"
        ATR_ARG="--fixed-atr-multiplier $ATR_OVERRIDE"
    else
        echo -e "  ${CYAN}  Brick-Größe: Optuna optimiert frei${NC}"
        ATR_ARG=""
    fi

    $PYTHON "$OPTIMIZER" \
        --pairs "${sym}|${tf}" \
        --start_date "$START_INPUT" \
        --end_date   "$END_INPUT" \
        --trials     "$TRIALS_INPUT" \
        --jobs       "$JOBS_INPUT" \
        --max_drawdown  "$DD_INPUT" \
        --start_capital "$CAPITAL_INPUT" \
        --min_win_rate  "$WR_INPUT" \
        --min_pnl 0 \
        --mode "$MODE_INPUT" \
        $ATR_ARG

done <<< "$PAIRS"

# ── 5. Portfolio-Optimierung ─────────────────────────────────────────────────
echo ""
echo "======================================================="
echo -e "  ${YELLOW}[Schritt 2/3] Portfolio-Optimierung${NC}"
echo "======================================================="
echo ""

$PYTHON "$SCRIPT_DIR/run_portfolio_optimizer.py" \
    --capital "$CAPITAL_INPUT" \
    --max-dd  "$DD_INPUT" \
    --start-date "$START_INPUT" \
    --end-date   "$END_INPUT"

# ── 6. Ergebnisse ─────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo -e "  ${YELLOW}[Schritt 3/3] Ergebnisse${NC}"
echo "======================================================="
echo ""

$PYTHON "$SCRIPT_DIR/src/zerobot/analysis/show_results.py" --mode 1

echo ""
echo "======================================================="
echo -e "  ${GREEN}Pipeline abgeschlossen!${NC}"
echo ""
echo "  Nächste Schritte:"
echo "    1. Ergebnisse prüfen:       ./show_results.sh"
echo "    2. Strategien aktivieren:   settings.json → \"active\": true"
echo "    3. Bot starten:             python3 master_runner.py"
echo "======================================================="

deactivate
