#!/bin/bash
# install.sh — Erstinstallation des ZeroBot (Renko) auf dem VPS
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "============================================================"
echo "  ZeroBot — Installation (Renko 2-Brick Reversal)"
echo "============================================================"
echo ""

# ── Python prüfen ─────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}FEHLER: python3 nicht gefunden!${NC}"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}✔ Python $PYTHON_VERSION gefunden${NC}"

# ── Virtuelle Umgebung ────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "Erstelle virtuelle Umgebung (.venv)..."
    python3 -m venv .venv
    echo -e "${GREEN}✔ Virtuelle Umgebung erstellt${NC}"
else
    echo -e "${GREEN}✔ Virtuelle Umgebung bereits vorhanden${NC}"
fi

# ── Abhängigkeiten installieren ───────────────────────────────────────────────
echo ""
echo "Installiere Abhängigkeiten..."
.venv/bin/pip install --upgrade pip --quiet

.venv/bin/pip install \
    ccxt \
    pandas \
    numpy \
    ta \
    optuna \
    tqdm \
    requests \
    --quiet

echo -e "${GREEN}✔ Abhängigkeiten installiert${NC}"

# Optionale Abhängigkeiten (für Charts und Excel)
echo ""
echo "Installiere optionale Abhängigkeiten (Charts, Excel)..."
.venv/bin/pip install plotly openpyxl matplotlib --quiet && \
    echo -e "${GREEN}✔ plotly + openpyxl + matplotlib installiert${NC}" || \
    echo -e "${YELLOW}⚠ plotly/openpyxl/matplotlib nicht installiert (Charts/Excel deaktiviert)${NC}"

# ── Ordnerstruktur anlegen ────────────────────────────────────────────────────
echo ""
echo "Erstelle Verzeichnisstruktur..."
mkdir -p artifacts/db artifacts/results logs data/cache
echo -e "${GREEN}✔ Verzeichnisse erstellt${NC}"

# ── Skripte ausführbar machen ─────────────────────────────────────────────────
chmod +x *.sh
echo -e "${GREEN}✔ Shell-Skripte sind ausführbar${NC}"

# ── secret.json prüfen ────────────────────────────────────────────────────────
echo ""
if [ ! -f "secret.json" ]; then
    echo "Erstelle secret.json Template..."
    cat > secret.json << 'EOF'
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
EOF
    echo -e "${YELLOW}⚠ secret.json wurde erstellt — bitte mit echten API-Keys befüllen!${NC}"
else
    echo -e "${GREEN}✔ secret.json bereits vorhanden${NC}"
fi

# ── Cronjob-Vorschlag ─────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo -e "  ${GREEN}Installation abgeschlossen!${NC}"
echo "============================================================"
echo ""
echo "Nächste Schritte:"
echo ""
echo -e "  ${CYAN}1. API-Keys eintragen:${NC}"
echo "     nano secret.json"
echo ""
echo -e "  ${CYAN}2. Symbole/Timeframes konfigurieren:${NC}"
echo "     nano settings.json"
echo ""
echo -e "  ${CYAN}3. Renko-Optimizer ausführen:${NC}"
echo "     ./run_pipeline.sh"
echo ""
echo -e "  ${CYAN}4. Live-Trading starten:${NC}"
echo "     .venv/bin/python3 master_runner.py"
echo ""
echo -e "  ${CYAN}5. Cronjob für automatischen Betrieb (alle 15 Min):${NC}"
echo "     crontab -e"
echo "     Eintragen:"
echo "     */15 * * * * cd $SCRIPT_DIR && .venv/bin/python3 master_runner.py >> logs/cron.log 2>&1"
echo ""
echo -e "  ${CYAN}Optional — Tests ausführen:${NC}"
echo "     ./run_tests.sh"
echo ""
echo -e "  ${CYAN}Optional — Ergebnisse anzeigen:${NC}"
echo "     ./show_results.sh"
echo ""
echo -e "  ${CYAN}Optional — Analysen:${NC}"
echo "     ./run_analysis.sh"
echo "============================================================"
