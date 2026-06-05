#!/bin/bash
# run_analysis.sh — ZeroBot Renko Wissenschaftliche Analysen
#
# Alle Analysen unter einem Befehl. Interaktive Auswahl.
#
# Ausführung:
#   ./run_analysis.sh
#   ./run_analysis.sh --no-telegram

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
NO_TELEGRAM=""

for arg in "$@"; do
    [[ "$arg" == "--no-telegram" ]] && NO_TELEGRAM="--no-telegram"
done

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

# ─── Menü ─────────────────────────────────────────────────────────────────────

echo ""
echo "======================================================="
echo -e "  ${BOLD}ZeroBot — Renko Analysen${NC}"
echo "======================================================="
echo ""
echo -e "  ${CYAN}── Strategie-Analyse ───────────────────────────────${NC}"
echo "   1) Einzel-Backtest            (alle Configs einzeln)"
echo "   2) Manuelle Portfolio-Sim.    (eigene Auswahl)"
echo "   3) Auto Portfolio-Optimierung (bestes Portfolio finden)"
echo ""
echo -e "  ${CYAN}── Renko-spezifische Analysen ──────────────────────${NC}"
echo "   4) Brick-Größen-Sweep         (ATR-Multiplier 0.5–2.5 testen)"
echo "   5) Trend-Längen-Analyse       (trend_min_bricks 2–6 vergleichen)"
echo "   6) Reversal-Bricks-Analyse    (reversal_bricks 1–4 vergleichen)"
echo "   7) Volumen-Filter Auswirkung  (mit / ohne vol_filter)"
echo ""
echo -e "  ${CYAN}── Risiko & Robustheit ─────────────────────────────${NC}"
echo "   8) RR-Ratio Sweep             (1.0–4.0 vergleichen)"
echo "   9) ATR-SL-Multiplier Sweep    (1.0–4.0 vergleichen)"
echo "  10) Timeframe-Vergleich        (1h vs 4h vs 6h)"
echo ""
echo "   0) Alle Analysen nacheinander (Standard-Werte)"
echo ""
read -p "Auswahl (0-10): " MODE
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
    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2024-01-01]: " SD
    SD="${SD//[$'\r\n ']/}"
    SD="${SD:-2024-01-01}"
    read -p "Enddatum   (JJJJ-MM-TT) [Standard: Heute]: " ED
    ED="${ED//[$'\r\n ']/}"
    ED="${ED:-$(date +%Y-%m-%d)}"
    echo "$SD $ED"
}

# ── Inline-Backtester für Sweeps ─────────────────────────────────────────────

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

# Lade alle verfügbaren Configs
configs_dir = os.path.join('$SCRIPT_DIR', 'src', 'zerobot', 'strategy', 'configs')
configs = []
if os.path.isdir(configs_dir):
    for fn in sorted(os.listdir(configs_dir)):
        if fn.startswith('config_') and fn.endswith('.json'):
            with open(os.path.join(configs_dir, fn)) as f:
                configs.append(json.load(f))

if not configs:
    print("Keine Configs gefunden. Zuerst run_pipeline.sh ausführen.")
    sys.exit(0)

config = configs[0]
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
    pnl = res.get('total_pnl_pct', 0)
    wr  = res.get('win_rate', 0)
    tr  = res.get('trades_count', 0)
    dd  = res.get('max_drawdown_pct', 0) * 100
    mark = ' ◀ BEST' if pnl == max(
        run_backtest(data.copy(),
            {**base_strat, param_name: v} if param_name in base_strat
                else {**base_strat}, {**risk, 'risk_reward_ratio': v} if param_name == 'rr_ratio'
                    else {**risk, 'atr_multiplier_sl': v} if param_name == 'atr_sl'
                        else {**risk}, capital).get('total_pnl_pct', 0)
        for v in values
    ) else ''
    print(f"  {str(val):<12} {tr:>8} {wr:>7.1f}% {pnl:>9.1f}% {dd:>9.1f}%{mark}")

print(f"{'─'*65}")
PYEOF
}

# ── Analyse-Modi ──────────────────────────────────────────────────────────────

run_mode() {
    local m="$1"
    case "$m" in

    1)  echo -e "${GREEN}▶ Einzel-Backtest${NC}"
        DATES=$(ask_dates)
        SD=$(echo $DATES | cut -d' ' -f1)
        ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        export ZB_START_DATE="$SD" ZB_END_DATE="$ED" ZB_CAPITAL="$CAP"
        $PYTHON -c "
import os,sys; sys.path.insert(0,'$SCRIPT_DIR/src')
from zerobot.analysis.show_results import run_single_analysis
run_single_analysis(os.environ['ZB_START_DATE'],os.environ['ZB_END_DATE'],int(float(os.environ['ZB_CAPITAL'])))
"
        ;;

    2)  echo -e "${GREEN}▶ Manuelle Portfolio-Simulation${NC}"
        DATES=$(ask_dates)
        SD=$(echo $DATES | cut -d' ' -f1)
        ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        export ZB_START_DATE="$SD" ZB_END_DATE="$ED" ZB_CAPITAL="$CAP"
        $PYTHON -c "
import os,sys; sys.path.insert(0,'$SCRIPT_DIR/src')
from zerobot.analysis.show_results import run_shared_mode
run_shared_mode(False,os.environ['ZB_START_DATE'],os.environ['ZB_END_DATE'],int(float(os.environ['ZB_CAPITAL'])),999.0)
"
        ;;

    3)  echo -e "${GREEN}▶ Auto Portfolio-Optimierung${NC}"
        DATES=$(ask_dates)
        SD=$(echo $DATES | cut -d' ' -f1)
        ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        read -p "Max. Drawdown in % [Standard: 30]: " DD
        DD="${DD//[$'\r\n ']/}"
        if ! [[ "$DD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then DD=30; fi
        $PYTHON "$SCRIPT_DIR/run_portfolio_optimizer.py" \
            --capital "$CAP" --max-dd "$DD" \
            --start-date "$SD" --end-date "$ED"
        ;;

    4)  echo -e "${GREEN}▶ Brick-Größen-Sweep (ATR-Multiplier 0.5–2.5)${NC}"
        echo "  Zeigt wie sich verschiedene Brick-Größen auf Performance auswirken."
        echo ""
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "atr_multiplier" "$CAP" "$SD" "$ED"
        ;;

    5)  echo -e "${GREEN}▶ Trend-Längen-Analyse (trend_min_bricks 2–6)${NC}"
        echo "  Wie viele Trend-Bricks brauchen wir für zuverlässige Signale?"
        echo ""
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "trend_min_bricks" "$CAP" "$SD" "$ED"
        ;;

    6)  echo -e "${GREEN}▶ Reversal-Bricks-Analyse (1–4 Bricks)${NC}"
        echo "  1 Brick = sofortiger Einstieg (mehr Trades, mehr Fehlsignale)"
        echo "  3+ Bricks = späte Bestätigung (weniger Trades, mehr Sicherheit)"
        echo ""
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "reversal_bricks" "$CAP" "$SD" "$ED"
        ;;

    7)  echo -e "${GREEN}▶ Volumen-Filter Auswirkung${NC}"
        echo "  Vergleicht Performance mit und ohne Volumen-Bestätigung."
        echo ""
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "vol_filter" "$CAP" "$SD" "$ED"
        ;;

    8)  echo -e "${GREEN}▶ RR-Ratio Sweep (1.0–4.0)${NC}"
        echo "  Findet das optimale Risiko-Rendite-Verhältnis für diese Renko-Strategie."
        echo ""
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "rr_ratio" "$CAP" "$SD" "$ED"
        ;;

    9)  echo -e "${GREEN}▶ ATR-SL-Multiplier Sweep (1.0–4.0)${NC}"
        echo "  Wie weit soll der SL vom Entry entfernt sein (in ATR-Vielfachen)?"
        echo ""
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        run_sweep "atr_sl" "$CAP" "$SD" "$ED"
        ;;

    10) echo -e "${GREEN}▶ Timeframe-Vergleich${NC}"
        echo "  Welcher Timeframe funktioniert am besten für Renko?"
        echo ""
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

    *)  echo -e "${RED}Ungültige Auswahl: $m${NC}" ;;
    esac
}

# ── Batch-Modus ───────────────────────────────────────────────────────────────

if [ "$MODE" == "0" ]; then
    echo -e "${YELLOW}▶ Alle Analysen mit Standard-Werten...${NC}"
    SD="2024-01-01"
    ED=$(date +%Y-%m-%d)
    CAP=100
    for i in 1 4 5 6 7 8 9 10; do
        echo ""
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}  Analyse $i${NC}"
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        case "$i" in
            1)  export ZB_START_DATE="$SD" ZB_END_DATE="$ED" ZB_CAPITAL="$CAP"
                $PYTHON -c "
import os,sys; sys.path.insert(0,'$SCRIPT_DIR/src')
from zerobot.analysis.show_results import run_single_analysis
run_single_analysis('$SD','$ED',$CAP)
" 2>/dev/null || true ;;
            4)  run_sweep "atr_multiplier"  "$CAP" "$SD" "$ED" 2>/dev/null || true ;;
            5)  run_sweep "trend_min_bricks" "$CAP" "$SD" "$ED" 2>/dev/null || true ;;
            6)  run_sweep "reversal_bricks"  "$CAP" "$SD" "$ED" 2>/dev/null || true ;;
            7)  run_sweep "vol_filter"        "$CAP" "$SD" "$ED" 2>/dev/null || true ;;
            8)  run_sweep "rr_ratio"          "$CAP" "$SD" "$ED" 2>/dev/null || true ;;
            9)  run_sweep "atr_sl"            "$CAP" "$SD" "$ED" 2>/dev/null || true ;;
            10) $PYTHON - <<PYEOF 2>/dev/null || true
import os,sys; sys.path.insert(0,'$SCRIPT_DIR/src')
from zerobot.analysis.backtester import load_data,run_backtest
import ta as ta_lib
symbol='DOGE/USDT:USDT'; capital=$CAP
for tf in ['1h','2h','4h','6h']:
    data=load_data(symbol,tf,'$SD','$ED')
    if data.empty: continue
    atr=ta_lib.volatility.AverageTrueRange(high=data['high'],low=data['low'],close=data['close'],window=14)
    data['atr']=atr.average_true_range(); data.dropna(subset=['atr'],inplace=True)
    strat={'atr_multiplier':1.0,'trend_min_bricks':3,'reversal_bricks':2,'vol_filter_enabled':True,'min_vol_ratio':1.2}
    risk={'risk_reward_ratio':2.0,'risk_per_trade_pct':1.0,'leverage':10,'trailing_stop_activation_rr':2.0,'trailing_stop_callback_rate_pct':1.0,'atr_multiplier_sl':2.0,'min_sl_pct':0.3}
    r=run_backtest(data.copy(),strat,risk,capital)
    print(f"  DOGE/{tf}: {r['trades_count']} Trades | WR {r['win_rate']:.1f}% | PnL {r['total_pnl_pct']:.1f}%")
PYEOF
            ;;
        esac
    done
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Alle Analysen abgeschlossen.${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
else
    run_mode "$MODE"
fi

deactivate
