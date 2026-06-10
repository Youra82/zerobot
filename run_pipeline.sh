#!/bin/bash
# run_pipeline.sh — ZeroBot EAR Optimierungs-Pipeline

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

echo ""
echo "======================================================="
echo "       ZeroBot EAR Optimierungs-Pipeline"
echo "  (Entropy-Adaptive Renko + N-Brick Trend Signal)"
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
read -p "Zeitfenster eingeben (z.B. 1h 4h) [leer=auto aus settings.json]: " TF_INPUT
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
    auto_tfs   = ['1h']

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

# ── Walk-Forward OOS-Test (optional) ─────────────────────────────────────────
echo ""
echo "======================================================="
echo "  Walk-Forward Out-of-Sample Test (optional)"
echo "======================================================="
echo ""
echo "  Konzept:"
echo "    1. Du wählst ein OOS-Datum (z.B. 2026-04-01)"
echo "    2. Optimizer trainiert NUR auf Daten VOR diesem Datum"
echo "    3. Danach testet der Backtester die Config auf Daten"
echo "       AB diesem Datum — exakt wie der Live-Bot (kein Lookahead)"
echo "    4. So erkennst du Overfitting sofort"
echo ""
read -p "OOS-Startdatum eingeben [leer=kein OOS-Test, Standard-Modus]: " OOS_DATE
OOS_DATE="${OOS_DATE//[$'\r\n ']/}"

if [[ -n "$OOS_DATE" ]]; then
    OPTIM_END=$($PYTHON -c "
from datetime import datetime, timedelta
d = datetime.strptime('$OOS_DATE', '%Y-%m-%d')
print((d - timedelta(days=1)).strftime('%Y-%m-%d'))
" 2>/dev/null)
    if [[ -z "$OPTIM_END" ]]; then
        echo -e "${RED}Ungültiges Datum '$OOS_DATE' — Standard-Modus.${NC}"
        OOS_DATE=""
        OPTIM_END=$(date +%Y-%m-%d)
    else
        OOS_TEST_END=$(date +%Y-%m-%d)

        # Vorschau-Startdatum berechnen (aus PAIRS + lookback_map)
        read PREVIEW_START PREVIEW_DAYS OOS_DAYS <<< $($PYTHON - <<PYEOF
from datetime import datetime, timedelta
pairs = """$PAIRS"""
lookback_map = {'15m':180,'30m':180,'1h':548,'2h':730,'4h':1095,'6h':1095,'1d':1825}
tfs = set()
for line in pairs.strip().split('\n'):
    parts = line.strip().split()
    if len(parts) == 2:
        tfs.add(parts[1])
days  = max((lookback_map.get(tf, 548) for tf in tfs), default=548)
ref   = datetime.strptime('$OPTIM_END', '%Y-%m-%d')
start = (ref - timedelta(days=days)).strftime('%Y-%m-%d')
d1    = datetime.strptime('$OOS_DATE',    '%Y-%m-%d')
d2    = datetime.strptime('$OOS_TEST_END','%Y-%m-%d')
print(start, days, (d2-d1).days)
PYEOF
        )

        echo ""
        echo -e "${GREEN}✔ OOS-Modus aktiv:${NC}"
        echo ""
        echo -e "  Trainingsperiode:  ${YELLOW}$PREVIEW_START${NC}  ──────────────────────►  ${YELLOW}$OPTIM_END${NC}"
        echo -e "  Backtestperiode:   ${CYAN}$OOS_DATE${NC}  ──────────────────────►  ${CYAN}$OOS_TEST_END${NC}  (dunkler Bereich)"
        echo ""
        echo    "  ────────────────────────────────────────────────────────────────────"
        echo -e "  ${YELLOW}◄──── TRAINING ($PREVIEW_DAYS Tage) ────►${NC}  ${CYAN}◄── BACKTEST ($OOS_DAYS Tage) ──►${NC}"
        echo    "  $PREVIEW_START              $OPTIM_END  $OOS_DATE        $OOS_TEST_END"
        echo    "  ────────────────────────────────────────────────────────────────────"
        echo ""
        echo -e "  ${CYAN}(Startdatum kann unten angepasst werden — 'a' = Automatik = $PREVIEW_START)${NC}"
    fi
else
    OPTIM_END=$(date +%Y-%m-%d)
    echo -e "${CYAN}ℹ  Standard-Modus — kein OOS-Test.${NC}"
fi

# ── Lookback-Empfehlung ───────────────────────────────────────────────────────
echo ""
echo "--- Empfehlung: Optimaler Rückblick-Zeitraum ---"
printf "+------------------+-----------------------------+\n"
printf "| %-16s | %-27s |\n" "Zeitfenster" "Empfohlener Rückblick (Tage)"
printf "+------------------+-----------------------------+\n"
printf "| %-16s | %-27s |\n" "15m, 30m"    "180 Tage   (~6 Monate)"
printf "| %-16s | %-27s |\n" "1h"          "548 Tage   (~1,5 Jahre)"
printf "| %-16s | %-27s |\n" "2h"          "730 Tage   (~2 Jahre)"
printf "| %-16s | %-27s |\n" "4h, 6h"      "1095 Tage  (~3 Jahre)"
printf "| %-16s | %-27s |\n" "1d"          "1825 Tage  (~5 Jahre)"
printf "+------------------+-----------------------------+\n"
if [[ -n "$OOS_DATE" ]]; then
    echo -e "  ${CYAN}Rückblick wird rückwärts ab OOS-Datum ($OOS_DATE) berechnet${NC}"
fi
echo ""

read -p "Startdatum (JJJJ-MM-TT) oder 'a' für Automatik [Standard: a]: " START_INPUT
START_INPUT="${START_INPUT//[$'\r\n ']/}"

# Enddatum für Optimizer = OOS-Datum - 1 Tag (oder heute wenn kein OOS)
END_INPUT="$OPTIM_END"

# Automatisches Startdatum — rückwärts ab OPTIM_END (nicht ab heute)
export ZB_OPTIM_END="$OPTIM_END"
if [[ -z "$START_INPUT" || "$START_INPUT" == "a" ]]; then
    START_INPUT=$($PYTHON - <<PYEOF2
import os
from datetime import datetime, timedelta
pairs = """$PAIRS"""
lookback_map = {'15m':180,'30m':180,'1h':548,'2h':730,'4h':1095,'6h':1095,'1d':1825}
tfs = set()
for line in pairs.strip().split('\n'):
    parts = line.strip().split()
    if len(parts) == 2:
        tfs.add(parts[1])
days = max((lookback_map.get(tf, 548) for tf in tfs), default=548)
ref = os.environ.get('ZB_OPTIM_END', '')
try:
    ref_dt = datetime.strptime(ref, '%Y-%m-%d')
except Exception:
    ref_dt = datetime.now()
print((ref_dt - timedelta(days=days)).strftime('%Y-%m-%d'))
PYEOF2
    )
fi

# ── Perioden-Zusammenfassung anzeigen ─────────────────────────────────────────
echo ""
if [[ -n "$OOS_DATE" ]]; then
    echo -e "  ${BOLD}Trainingsperiode:  ${GREEN}$START_INPUT  →  $END_INPUT${NC}"
    echo -e "  ${BOLD}Backtestperiode:   ${YELLOW}ab $OOS_DATE  →  $OOS_TEST_END  (dunkler Bereich)${NC}"
else
    echo -e "  ${BOLD}Backtestperiode:   ${GREEN}$START_INPUT  →  $END_INPUT${NC}"
fi
echo ""

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
echo "  1) Strenger Modus    (Profitabel + WR >= Min. Win-Rate + MaxDD <= Limit)"
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

# ── EAR Strategie-Parameter (optional fixieren) ───────────────────────────────
echo ""
echo "--- EAR Signal-Parameter ---"
echo ""
echo "  Was Optuna optimiert wenn du leer lässt:"
printf "  %-22s %s\n" "base_pct"         "Basis-Brick-Größe in % des Preises      (0.002–0.010)"
printf "  %-22s %s\n" "k_entropy"        "Entropie-Gewichtung der Brick-Size      (0.4–1.5)"
printf "  %-22s %s\n" "h_window"         "Glättungs-Fenster für Entropie          (5–20)"
printf "  %-22s %s\n" "trend_min_bricks" "Mindest-Bricks in gleicher Richtung     (2–6)"
echo ""
echo "  Zahl/Wert eingeben → wird fixiert | leer → Optuna optimiert frei"
echo ""

read_ear_param() {
    local NAME="$1"; local DEFAULT="$2"; local VAR_NAME="$3"
    read -p "  $NAME [Empfehlung: $DEFAULT, leer=Optuna frei]: " VAL
    VAL="${VAL//[$'\r\n ']/}"
    if [[ "$VAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        eval "$VAR_NAME=$VAL"
        echo -e "    ${GREEN}-> Fixiert auf: $VAL${NC}"
    else
        eval "$VAR_NAME="
        echo -e "    ${CYAN}-> Optuna optimiert frei${NC}"
    fi
}

read_ear_param "base_pct"         "0.004" BASE_PCT_ARG
read_ear_param "k_entropy"        "0.8"   K_ENTROPY_ARG
read_ear_param "h_window"         "10"    H_WINDOW_ARG
read_ear_param "trend_min_bricks" "3"     TREND_MIN_BRICKS_ARG

# ── Optimizer pro Pair ────────────────────────────────────────────────────────
OPTIMIZER="$SCRIPT_DIR/src/zerobot/analysis/optimizer.py"

while IFS=' ' read -r sym tf; do
    COIN=$(echo "$sym" | cut -d'/' -f1)

    echo ""
    echo "======================================================="
    echo "  Bearbeite Pipeline für: $COIN ($tf)"
    if [[ -n "$OOS_DATE" ]]; then
        echo "  Trainingszeitraum: $START_INPUT bis $END_INPUT"
        echo "  OOS-Periode:       $OOS_DATE bis $OOS_TEST_END"
    else
        echo "  Datenzeitraum: $START_INPUT bis $END_INPUT"
    fi
    echo "======================================================="
    echo ""

    EXTRA_ARGS=""
    [ -n "$BASE_PCT_ARG"          ] && EXTRA_ARGS="$EXTRA_ARGS --fixed-base-pct $BASE_PCT_ARG"
    [ -n "$K_ENTROPY_ARG"         ] && EXTRA_ARGS="$EXTRA_ARGS --fixed-k-entropy $K_ENTROPY_ARG"
    [ -n "$H_WINDOW_ARG"          ] && EXTRA_ARGS="$EXTRA_ARGS --fixed-h-window $H_WINDOW_ARG"
    [ -n "$TREND_MIN_BRICKS_ARG"  ] && EXTRA_ARGS="$EXTRA_ARGS --fixed-trend-min-bricks $TREND_MIN_BRICKS_ARG"

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

# ── Walk-Forward OOS-Test (wenn OOS-Datum gesetzt) ────────────────────────────
if [[ -n "$OOS_DATE" ]]; then
    echo ""
    echo "======================================================="
    echo -e "  ${CYAN}Walk-Forward OOS-Test: $OOS_DATE → $OOS_TEST_END${NC}"
    echo "  (Live-Bot-Simulation auf dem dunklen Bereich)"
    echo "======================================================="
    echo ""

    OOS_TESTER="$SCRIPT_DIR/src/zerobot/analysis/oos_tester.py"
    $PYTHON "$OOS_TESTER" \
        --oos_start    "$OOS_DATE" \
        --oos_end      "$OOS_TEST_END" \
        --warmup_start "$START_INPUT" \
        --start_capital "$CAPITAL_INPUT"
fi

echo ""
echo "======================================================="
echo -e "  ${CYAN}Brick-State-Initialisierung (Live-Bot Startpunkt)${NC}"
echo "======================================================="
echo ""
$PYTHON "$SCRIPT_DIR/init_brick_states.py" --force

echo ""
echo "======================================================="
echo -e "  ${GREEN}Pipeline abgeschlossen!${NC}"
echo ""
if [[ -n "$OOS_DATE" ]]; then
    echo "  OOS-Ergebnisse:      artifacts/results/last_oos_run.json"
fi
echo "  Nächste Schritte:"
echo "    1. Ergebnisse prüfen:    ./show_results.sh"
echo "    2. settings.json:        \"active\": true setzen"
echo "    3. Bot starten:          python3 master_runner.py"
echo "======================================================="

deactivate
