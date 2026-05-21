#!/bin/bash
# show_results.sh — Interaktives Analyse-Menü für zerobot

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
VENV_PATH="$SCRIPT_DIR/.venv/bin/activate"

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst installieren:${NC}"
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi
source "$VENV_PATH"

echo ""
echo -e "${YELLOW}Wähle einen Analyse-Modus:${NC}"
echo "  1) Einzel-Backtest               (jedes Pair wird simuliert)"
echo "  2) Manuelle Portfolio-Simulation (du wählst die Pairs)"
echo "  3) Parameter-Optimizer           (Optuna tuned Physics-Parameter)"
echo "  4) Quantum State Bibliothek      (Top-Muster + Stats aus der DB)"
echo "  5) Interaktive Charts            (Candlestick + Entry/Exit-Marker)"
read -p "Auswahl (1-5) [Standard: 4]: " MODE

if [[ ! "$MODE" =~ ^[1-5]?$ ]]; then
    echo -e "${RED}Ungültige Eingabe. Verwende Standard (4).${NC}"
    MODE=4
fi
MODE=${MODE:-4}

# ── Hilfsfunktion: Zusammenfassung aus gespeicherten JSON-Ergebnissen ────────
show_summary() {
    # $1 = mehrzeiliger String "SYM TF\nSYM TF\n..."
    # $2 = Startkapital (für PnL%-Berechnung im Header)
    local pairs="$1"
    local capital="${2:-50}"
    echo "$pairs" | ZEROBOT_CAPITAL="$capital" $PYTHON - <<'PYEOF'
import os, sys, json

results_dir = 'artifacts/results'
capital     = float(os.environ.get('ZEROBOT_CAPITAL', '50'))

G  = '\033[0;32m'
Y  = '\033[1;33m'
R  = '\033[0;31m'
C  = '\033[0;36m'
B  = '\033[1;37m'
NC = '\033[0m'

rows = []
for line in sys.stdin:
    sym_tf = line.strip()
    if not sym_tf:
        continue
    parts = sym_tf.split()
    if len(parts) < 2:
        continue
    sym, tf = parts[0], parts[1]
    safe = sym.replace('/', '').replace(':', '')

    # Test-Ergebnis bevorzugen, sonst Train
    stats = None
    for label, suffix in [('TEST', f'{safe}_{tf}_test'), ('TRAIN', f'{safe}_{tf}_train')]:
        path = os.path.join(results_dir, f'backtest_{suffix}.json')
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                stats = data.get('stats', {})
                if stats.get('total_trades', 0) > 0:
                    rows.append((sym, tf, label, stats))
                    break
            except Exception:
                pass

w = 76
print(f"\n{'=' * w}")
print(f"{B}  ZUSAMMENFASSUNG — alle Pairs{NC}")
print(f"{'=' * w}")
print(f"{C}  {'Markt':<22} {'TF':<5} {'Trades':>7} {'WR':>7} {'PnL%':>8} {'PF':>6} {'MaxDD':>7}{NC}")
print(f"  {'-' * (w - 2)}")

rows.sort(key=lambda x: x[3].get('total_pnl_pct', 0), reverse=True)
for sym, tf, lbl, st in rows:
    n    = st['total_trades']
    wr   = st['win_rate']
    pnl  = st['total_pnl_pct']
    pf   = st.get('profit_factor', 0)
    dd   = st.get('max_drawdown_pct', 0)
    sign = '+' if pnl >= 0 else ''
    pf_str = f'{pf:.2f}' if pf != float('inf') else '∞'

    pnl_col = G if pnl > 0 else (Y if pnl == 0 else R)
    wr_col  = G if wr >= 0.50 else (Y if wr >= 0.43 else R)

    print(
        f"  {sym:<22} {tf:<5} "
        f"{n:>7} "
        f"{wr_col}{wr:>6.1%}{NC} "
        f"{pnl_col}{sign}{pnl:>6.1f}%{NC} "
        f"{pf_str:>6} "
        f"{dd:>6.1f}%"
    )
print(f"{'=' * w}\n")
PYEOF
}

# ─────────────────────────────────────────
# Mode 1: Einzel-Backtest
# ─────────────────────────────────────────
if [ "$MODE" == "1" ]; then
    echo ""
    read -p "Coin(s) eingeben (z.B. BTC ETH SOL) [leer=alle aus DB]: " COINS_INPUT
    COINS_INPUT="${COINS_INPUT//[$'\r\n']/}"
    read -p "Timeframe(s) eingeben (z.B. 4h 1h) [leer=alle aus DB]: " TF_INPUT
    TF_INPUT="${TF_INPUT//[$'\r\n']/}"

    read -p "Startkapital in USDT [Standard: 50]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=50; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    read -p "Startdatum (JJJJ-MM-TT) [leer=alles]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"

    read -p "Enddatum (JJJJ-MM-TT) [leer=Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"

    DATE_ARGS=""
    [ -n "$START_DATE" ] && DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ]   && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    if [ -z "$COINS_INPUT" ] && [ -z "$TF_INPUT" ]; then
        # Alle Pairs aus DB
        $PYTHON "$SCRIPT_DIR/run_backtest.py" \
            --capital "$CAPITAL" --risk "$RISK" $DATE_ARGS
    else
        # Spezifische Pairs aufbauen
        PAIRS=$(ZEROBOT_SHOW_COINS="$COINS_INPUT" ZEROBOT_SHOW_TFS="$TF_INPUT" \
            $PYTHON - <<'PYEOF'
import os, json

coins_raw = os.environ.get('ZEROBOT_SHOW_COINS', '').strip()
tfs_raw   = os.environ.get('ZEROBOT_SHOW_TFS', '').strip()

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

        echo "$PAIRS" | while IFS=' ' read -r sym tf; do
            [ -z "$sym" ] && continue
            echo -e "${CYAN}  Backtest: $sym ($tf)${NC}"
            $PYTHON "$SCRIPT_DIR/run_backtest.py" \
                --symbol "$sym" --timeframe "$tf" \
                --capital "$CAPITAL" --risk "$RISK" $DATE_ARGS
        done
        show_summary "$PAIRS" "$CAPITAL"
    fi

# ─────────────────────────────────────────
# Mode 2: Manuelle Portfolio-Simulation
# ─────────────────────────────────────────
elif [ "$MODE" == "2" ]; then
    echo ""
    read -p "Startkapital in USDT [Standard: 50]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=50; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    read -p "Startdatum (JJJJ-MM-TT) [leer=alles]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"

    read -p "Enddatum (JJJJ-MM-TT) [leer=Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"

    DATE_ARGS=""
    [ -n "$START_DATE" ] && DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ]   && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    $PYTHON "$SCRIPT_DIR/run_manual_portfolio.py" \
        --capital "$CAPITAL" --risk "$RISK" $DATE_ARGS

# ─────────────────────────────────────────
# Mode 3: Automatische Portfolio-Opt.
# ─────────────────────────────────────────
elif [ "$MODE" == "3" ]; then
    echo ""
    read -p "Gewünschter maximaler Drawdown in % [Standard: 30]: " MAX_DD
    MAX_DD="${MAX_DD//[$'\r\n ']/}"
    if ! [[ "$MAX_DD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then MAX_DD=30; fi

    echo ""
    echo "--- Bitte Konfiguration festlegen ---"
    read -p "Startdatum (JJJJ-MM-TT) [leer=alles]: " START_DATE
    START_DATE="${START_DATE//[$'\r\n ']/}"

    read -p "Enddatum (JJJJ-MM-TT) [leer=Heute]: " END_DATE
    END_DATE="${END_DATE//[$'\r\n ']/}"

    read -p "Startkapital in USDT [Standard: 50]: " CAPITAL
    CAPITAL="${CAPITAL//[$'\r\n ']/}"
    if ! [[ "$CAPITAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAPITAL=50; fi

    read -p "Risiko pro Trade in % [Standard: 1.0]: " RISK
    RISK="${RISK//[$'\r\n ']/}"
    if ! [[ "$RISK" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then RISK=1.0; fi

    DATE_ARGS=""
    [ -n "$START_DATE" ] && DATE_ARGS="--start-date $START_DATE"
    [ -n "$END_DATE" ]   && DATE_ARGS="$DATE_ARGS --end-date $END_DATE"

    echo ""
    $PYTHON "$SCRIPT_DIR/run_portfolio_optimizer.py" \
        --capital "$CAPITAL" --risk "$RISK" --max-dd "$MAX_DD" $DATE_ARGS

# ─────────────────────────────────────────
# Mode 5: Interaktive Charts
# ─────────────────────────────────────────
elif [ "$MODE" == "5" ]; then
    echo ""
    $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/show_results.py" --mode 4

# ─────────────────────────────────────────
# Mode 4: Quantum State Bibliothek (Standard)
# ─────────────────────────────────────────
else
    $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/show_results.py" --mode 1
fi

deactivate
