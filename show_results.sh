#!/bin/bash
# show_results.sh — ZeroBot Renko Ergebnisanzeige

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

echo ""
echo -e "${YELLOW}Wähle einen Modus:${NC}"
echo "  1) Einzel-Backtest               (jede Config einzeln simuliert)"
echo "  2) Manuelle Portfolio-Simulation (du wählst die Strategien)"
echo "  3) Automatische Portfolio-Opt.   (Bot wählt das beste Portfolio)"
echo ""
read -p "Auswahl (1-3) [Standard: 1]: " MODE
MODE="${MODE//[$'\r\n ']/}"
MODE="${MODE:-1}"

if [[ ! "$MODE" =~ ^[1-3]$ ]]; then
    echo -e "${RED}Ungültige Eingabe. Verwende Modus 1.${NC}"
    MODE=1
fi

# ── Zeitraum und Kapital abfragen ─────────────────────────────────────────────
echo ""
read -p "Startdatum (JJJJ-MM-TT) [Standard: 2024-01-01]: " START
START="${START//[$'\r\n ']/}"
START="${START:-2024-01-01}"

read -p "Enddatum   (JJJJ-MM-TT) [Standard: Heute]: " END
END="${END//[$'\r\n ']/}"
END="${END:-$(date +%Y-%m-%d)}"

read -p "Startkapital in USDT [Standard: 100]: " CAP
CAP="${CAP//[$'\r\n ']/}"
if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi

echo ""

if [ "$MODE" == "3" ]; then
    read -p "Max. Drawdown in % [Standard: 30]: " MAX_DD
    MAX_DD="${MAX_DD//[$'\r\n ']/}"
    if ! [[ "$MAX_DD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then MAX_DD=30; fi

    $PYTHON "$SCRIPT_DIR/run_portfolio_optimizer.py" \
        --capital "$CAP" \
        --max-dd  "$MAX_DD" \
        --start-date "$START" \
        --end-date   "$END"
else
    # Übergabe via Env-Variablen an show_results.py (liest stdin)
    export ZB_START_DATE="$START"
    export ZB_END_DATE="$END"
    export ZB_CAPITAL="$CAP"

    $PYTHON - <<PYEOF
import os, sys
sys.path.insert(0, os.path.join('$SCRIPT_DIR', 'src'))
from zerobot.analysis.show_results import run_single_analysis, run_shared_mode

start   = os.environ.get('ZB_START_DATE', '2024-01-01')
end     = os.environ.get('ZB_END_DATE',   '$END')
capital = float(os.environ.get('ZB_CAPITAL', '100'))
mode    = '$MODE'

if mode == '1':
    run_single_analysis(start, end, int(capital))
elif mode == '2':
    run_shared_mode(False, start, end, int(capital), 999.0)
PYEOF
fi

deactivate
