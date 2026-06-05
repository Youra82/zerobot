#!/bin/bash
# run_analysis.sh — ZeroBot Renko Wissenschaftliche Analysen

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausfuehren!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH}"

# ─── Menue ────────────────────────────────────────────────────────────────────

echo ""
echo "======================================================="
echo -e "  ${BOLD}ZeroBot — Renko Wissenschaftliche Analysen${NC}"
echo "======================================================="
echo ""
echo -e "  ${CYAN}── Prioritaet 1: Fundament ─────────────────────────${NC}"
echo "   1) Walk-Forward Out-of-Sample Test"
echo "   2) Slippage & Fee Impact"
echo "   3) Monte Carlo Simulation"
echo "   4) Bootstrap Signifikanztest"
echo ""
echo -e "  ${CYAN}── Prioritaet 2: Parameter-Optimierung ─────────────${NC}"
echo "   5) RR-Ratio Walk-Forward"
echo "   6) ATR-SL-Multiplier Walk-Forward"
echo "   7) Trailing Callback Walk-Forward"
echo "   8) Parameter Sensitivity (Tornado-Diagramm)"
echo ""
echo -e "  ${CYAN}── Prioritaet 3: Systemverbesserung ─────────────────${NC}"
echo "   9) Multi-Timeframe Confirmation"
echo "  10) Parameter-Stabilitaets-Analyse"
echo "  11) Anti-Korrelations-Portfolio"
echo "  12) Kelly Position Sizing"
echo ""
echo -e "  ${CYAN}── Prioritaet 4-6: Feintuning ───────────────────────${NC}"
echo "  13) Regime Performance Analysis"
echo "  14) Brick-Pattern-Kombinations-Analyse"
echo "  15) Confluence Score"
echo "  16) Volatilitaets-Filter Optimierung"
echo "  17) Tageszeit-Analyse"
echo "  18) Regime-adaptive Parameter"
echo "  19) Drawdown Duration Analysis"
echo ""
echo -e "  ${CYAN}── Renko-Schnell-Sweeps ────────────────────────────${NC}"
echo "  20) Brick-Groessen-Sweep     (ATR-Multiplier 0.5-2.5)"
echo "  21) Trend-Laengen-Sweep      (trend_min_bricks 2-6)"
echo "  22) Reversal-Bricks-Sweep    (reversal_bricks 1-4)"
echo "  23) Volumen-Filter Vergleich"
echo "  24) Timeframe-Vergleich"
echo ""
echo "   0) Alle 1-19 Analysen nacheinander"
echo ""
read -p "Auswahl (0-24): " MODE
MODE="${MODE//[$'\r\n ']/}"
echo ""

# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

ask_capital() {
    read -p "Startkapital in USDT [Standard: 100]: " CAP
    CAP="${CAP//[$'\r\n ']/}"
    if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
    echo "$CAP"
}

ask_dates() {
    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: " SD
    SD="${SD//[$'\r\n ']/}"
    SD="${SD:-2023-01-01}"
    read -p "Enddatum   (JJJJ-MM-TT) [Standard: Heute]: " ED
    ED="${ED//[$'\r\n ']/}"
    ED="${ED:-$(date +%Y-%m-%d)}"
    echo "$SD $ED"
}

# ── Inline-Sweep Funktion (fuer Items 20-24) ─────────────────────────────────

run_sweep() {
    local SWEEP_TYPE="$1"
    local CAP="$2"
    local SD="$3"
    local ED="$4"

    $PYTHON - <<PYEOF
import os, sys, json
sys.path.insert(0, os.path.join('$SCRIPT_DIR', 'src'))
from zerobot.analysis.backtester import load_data, run_backtest
import ta

sweep_type = '$SWEEP_TYPE'
capital    = $CAP
start_date = '$SD'
end_date   = '$ED'

configs_dir = os.path.join('$SCRIPT_DIR', 'src', 'zerobot', 'strategy', 'configs')
configs = []
if os.path.isdir(configs_dir):
    for fn in sorted(os.listdir(configs_dir)):
        if fn.startswith('config_') and fn.endswith('.json'):
            with open(os.path.join(configs_dir, fn)) as f:
                configs.append(json.load(f))

if not configs:
    print("Keine Configs gefunden. Zuerst run_pipeline.sh ausfuehren.")
    sys.exit(0)

config    = configs[0]
symbol    = config['market']['symbol']
timeframe = config['market']['timeframe']
risk      = config.get('risk', {})

print(f"Sweep auf: {symbol} ({timeframe})")
data = load_data(symbol, timeframe, start_date, end_date)
if data.empty:
    print("Keine Daten.")
    sys.exit(0)

import ta as ta_lib
atr_ind = ta_lib.volatility.AverageTrueRange(
    high=data['high'], low=data['low'], close=data['close'], window=14)
data['atr'] = atr_ind.average_true_range()
data.dropna(subset=['atr'], inplace=True)

base_strat = dict(config.get('strategy', {}))

if sweep_type == 'atr_multiplier':
    values = [0.5, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5]
    param_name = 'atr_multiplier'
elif sweep_type == 'trend_min_bricks':
    values = [2, 3, 4, 5, 6]
    param_name = 'trend_min_bricks'
elif sweep_type == 'reversal_bricks':
    values = [1, 2, 3, 4]
    param_name = 'reversal_bricks'
elif sweep_type == 'vol_filter':
    values = [True, False]
    param_name = 'vol_filter_enabled'
elif sweep_type == 'rr_ratio':
    values = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    param_name = 'rr_ratio'
elif sweep_type == 'atr_sl':
    values = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    param_name = 'atr_sl'

print(f"\n{'─'*65}")
print(f"  {'Wert':<12} {'Trades':>8} {'Win%':>8} {'PnL%':>10} {'MaxDD%':>10}")
print(f"{'─'*65}")

all_pnls = []
for val in values:
    strat = dict(base_strat)
    r = dict(risk)
    if param_name in ('atr_multiplier','trend_min_bricks','reversal_bricks','vol_filter_enabled'):
        strat[param_name] = val
    elif param_name == 'rr_ratio':
        r['risk_reward_ratio'] = val
    elif param_name == 'atr_sl':
        r['atr_multiplier_sl'] = val
    res = run_backtest(data.copy(), strat, r, capital, verbose=False)
    all_pnls.append(res.get('total_pnl_pct', 0))

best_pnl = max(all_pnls) if all_pnls else 0

for idx, val in enumerate(values):
    strat = dict(base_strat)
    r = dict(risk)
    if param_name in ('atr_multiplier','trend_min_bricks','reversal_bricks','vol_filter_enabled'):
        strat[param_name] = val
    elif param_name == 'rr_ratio':
        r['risk_reward_ratio'] = val
    elif param_name == 'atr_sl':
        r['atr_multiplier_sl'] = val
    res = run_backtest(data.copy(), strat, r, capital, verbose=False)
    pnl = res.get('total_pnl_pct', 0)
    wr  = res.get('win_rate', 0)
    tr  = res.get('trades_count', 0)
    dd  = res.get('max_drawdown_pct', 0) * 100
    mark = ' <-- BEST' if abs(pnl - best_pnl) < 0.001 else ''
    print(f"  {str(val):<12} {tr:>8} {wr:>7.1f}% {pnl:>9.1f}% {dd:>9.1f}%{mark}")

print(f"{'─'*65}")
PYEOF
}

# ── Analyse-Modi ──────────────────────────────────────────────────────────────

run_mode() {
    local m="$1"
    local SD="${2:-2023-01-01}"
    local ED="${3:-$(date +%Y-%m-%d)}"
    local CAP="${4:-100}"
    local SIMS="${5:-5000}"
    local WH="${6:-4}"
    local MIN_T="${7:-10}"

    case "$m" in

    1)  echo -e "${GREEN}▶ Walk-Forward Out-of-Sample Test${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/walk_forward.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    2)  echo -e "${GREEN}▶ Slippage & Fee Impact${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/fee_impact.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    3)  echo -e "${GREEN}▶ Monte Carlo Simulation${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
            read -p "Anzahl Simulationen [Standard: 5000]: " SIMS
            SIMS="${SIMS//[$'\r\n ']/}"
            if ! [[ "$SIMS" =~ ^[0-9]+$ ]]; then SIMS=5000; fi
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/monte_carlo.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" --simulations "$SIMS"
        ;;

    4)  echo -e "${GREEN}▶ Bootstrap Signifikanztest${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            read -p "Minimale Trades [Standard: 10]: " MIN_T
            MIN_T="${MIN_T//[$'\r\n ']/}"
            if ! [[ "$MIN_T" =~ ^[0-9]+$ ]]; then MIN_T=10; fi
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/bootstrap_test.py" \
            --start-date "$SD" --end-date "$ED" --min-trades "$MIN_T"
        ;;

    5)  echo -e "${GREEN}▶ RR-Ratio Walk-Forward${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sweep_walkforward.py" \
            --param rr --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    6)  echo -e "${GREEN}▶ ATR-SL-Multiplier Walk-Forward${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sweep_walkforward.py" \
            --param atr_sl --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    7)  echo -e "${GREEN}▶ Trailing Callback Walk-Forward${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sweep_walkforward.py" \
            --param trailing --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    8)  echo -e "${GREEN}▶ Parameter Sensitivity (Tornado-Diagramm)${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sensitivity.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    9)  echo -e "${GREEN}▶ Multi-Timeframe Confirmation${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
            read -p "Gleichzeitigkeits-Fenster in Stunden [Standard: 4]: " WH
            WH="${WH//[$'\r\n ']/}"
            if ! [[ "$WH" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then WH=4; fi
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/multitf_analysis.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH"
        ;;

    10) echo -e "${GREEN}▶ Parameter-Stabilitaets-Analyse${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_stability.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    11) echo -e "${GREEN}▶ Anti-Korrelations-Portfolio${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/correlation.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    12) echo -e "${GREEN}▶ Kelly Position Sizing${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/kelly_sizing.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    13) echo -e "${GREEN}▶ Regime Performance Analysis${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/regime_analysis.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    14) echo -e "${GREEN}▶ Brick-Pattern-Kombinations-Analyse${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/brick_pattern.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    15) echo -e "${GREEN}▶ Confluence Score${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
            read -p "Gleichzeitigkeits-Fenster in Stunden [Standard: 4]: " WH
            WH="${WH//[$'\r\n ']/}"
            if ! [[ "$WH" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then WH=4; fi
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/confluence.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH"
        ;;

    16) echo -e "${GREEN}▶ Volatilitaets-Filter Optimierung${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/vol_filter.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    17) echo -e "${GREEN}▶ Tageszeit-Analyse${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/time_analysis.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    18) echo -e "${GREEN}▶ Regime-adaptive Parameter${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/regime_adaptive.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    19) echo -e "${GREEN}▶ Drawdown Duration Analysis${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/drawdown_duration.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP"
        ;;

    20) echo -e "${GREEN}▶ Brick-Groessen-Sweep (ATR-Multiplier 0.5-2.5)${NC}"
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "atr_multiplier" "$CAP" "$SD" "$ED"
        ;;

    21) echo -e "${GREEN}▶ Trend-Laengen-Sweep (trend_min_bricks 2-6)${NC}"
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "trend_min_bricks" "$CAP" "$SD" "$ED"
        ;;

    22) echo -e "${GREEN}▶ Reversal-Bricks-Sweep (1-4 Bricks)${NC}"
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "reversal_bricks" "$CAP" "$SD" "$ED"
        ;;

    23) echo -e "${GREEN}▶ Volumen-Filter Vergleich${NC}"
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "vol_filter" "$CAP" "$SD" "$ED"
        ;;

    24) echo -e "${GREEN}▶ Timeframe-Vergleich${NC}"
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        read -p "Coin (z.B. DOGE) [Standard: DOGE]: " COIN_INPUT
        COIN_INPUT="${COIN_INPUT//[$'\r\n ']/}"
        COIN_INPUT="${COIN_INPUT:-DOGE}"
        SYMBOL="${COIN_INPUT^^}/USDT:USDT"

        $PYTHON - <<PYEOF2
import os, sys, json
sys.path.insert(0, '$SCRIPT_DIR/src')
from zerobot.analysis.backtester import load_data, run_backtest
import ta as ta_lib

symbol  = '$SYMBOL'
capital = $CAP
sd, ed  = '$SD', '$ED'
tfs     = ['1h', '2h', '4h', '6h', '1d']

print(f"\nTimeframe-Vergleich: {symbol}")
print(f"{'─'*65}")
print(f"  {'TF':<8} {'Trades':>8} {'Win%':>8} {'PnL%':>10} {'MaxDD%':>10}")
print(f"{'─'*65}")

for tf in tfs:
    data = load_data(symbol, tf, sd, ed)
    if data.empty:
        print(f"  {tf:<8} {'—':>8}")
        continue
    atr_ind = ta_lib.volatility.AverageTrueRange(
        high=data['high'], low=data['low'], close=data['close'], window=14)
    data['atr'] = atr_ind.average_true_range()
    data.dropna(subset=['atr'], inplace=True)
    strat = {'atr_multiplier': 1.0, 'trend_min_bricks': 3, 'reversal_bricks': 2,
             'vol_filter_enabled': True, 'min_vol_ratio': 1.2}
    risk  = {'risk_reward_ratio': 2.0, 'risk_per_trade_pct': 1.0, 'leverage': 10,
             'trailing_stop_activation_rr': 2.0, 'trailing_stop_callback_rate_pct': 1.0,
             'atr_multiplier_sl': 2.0, 'min_sl_pct': 0.3}
    res = run_backtest(data.copy(), strat, risk, capital, verbose=False)
    print(f"  {tf:<8} {res['trades_count']:>8} {res['win_rate']:>7.1f}% "
          f"{res['total_pnl_pct']:>9.1f}% {res['max_drawdown_pct']*100:>9.1f}%")

print(f"{'─'*65}")
PYEOF2
        ;;

    *)  echo -e "${RED}Ungueltige Auswahl: $m${NC}" ;;
    esac
}

# ── Batch-Modus (alle 1-19) ───────────────────────────────────────────────────

if [ "$MODE" == "0" ]; then
    echo -e "${YELLOW}▶ Alle Analysen 1-19 mit Standard-Werten...${NC}"
    SD="2023-01-01"
    ED=$(date +%Y-%m-%d)
    CAP=100
    SIMS=5000
    WH=4
    MIN_T=10

    for i in $(seq 1 19); do
        echo ""
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}  Analyse $i von 19${NC}"
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        case "$i" in
            1)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/walk_forward.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            2)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/fee_impact.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            3)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/monte_carlo.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" --simulations "$SIMS" 2>/dev/null || true ;;
            4)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/bootstrap_test.py" \
                    --start-date "$SD" --end-date "$ED" --min-trades "$MIN_T" 2>/dev/null || true ;;
            5)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sweep_walkforward.py" \
                    --param rr --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            6)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sweep_walkforward.py" \
                    --param atr_sl --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            7)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sweep_walkforward.py" \
                    --param trailing --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            8)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_sensitivity.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            9)  $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/multitf_analysis.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH" 2>/dev/null || true ;;
            10) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/param_stability.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            11) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/correlation.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            12) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/kelly_sizing.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            13) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/regime_analysis.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            14) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/brick_pattern.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            15) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/confluence.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH" 2>/dev/null || true ;;
            16) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/vol_filter.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            17) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/time_analysis.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            18) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/regime_adaptive.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
            19) $PYTHON "$SCRIPT_DIR/src/zerobot/analysis/drawdown_duration.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" 2>/dev/null || true ;;
        esac
    done
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Alle 19 Analysen abgeschlossen.${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
else
    run_mode "$MODE"
fi

deactivate
