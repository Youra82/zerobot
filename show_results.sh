#!/bin/bash
# show_results.sh — Interaktives Analyse-Menü für zerobot

cd "$(dirname "$0")"

while true; do
    echo ""
    echo "=== zerobot Analyse-Menü ==="
    echo "1) Backtest (70/30 Split)"
    echo "2) Backtest — nur Test-Periode"
    echo "3) Backtest — bestimmtes Pair"
    echo "4) Optimizer starten (Optuna, 50 Trials)"
    echo "5) Optimizer mit Auto-Write"
    echo "6) Quantum State Library anzeigen"
    echo "0) Beenden"
    echo ""
    read -p "Auswahl: " choice

    case $choice in
        1)
            .venv/bin/python3 run_backtest.py --capital 50
            ;;
        2)
            .venv/bin/python3 run_backtest.py --capital 50 --test-only
            ;;
        3)
            read -p "Symbol (z.B. BTC/USDT:USDT): " sym
            read -p "Timeframe (z.B. 4h): " tf
            .venv/bin/python3 run_backtest.py --symbol "$sym" --timeframe "$tf" --capital 50
            ;;
        4)
            .venv/bin/python3 run_optimizer.py --trials 50 --capital 50
            ;;
        5)
            .venv/bin/python3 run_optimizer.py --trials 50 --capital 50 --auto-write
            ;;
        6)
            .venv/bin/python3 -c "
from zerobot.physics.database import StateDB
from zerobot.physics.evolver import print_state_report
import json, os
s = json.load(open('settings.json'))
pairs = [(p['symbol'], p['timeframe']) for p in s['live_trading_settings']['active_strategies']]
db = StateDB('artifacts/db/quantum.db')
print_state_report(db)
db.close()
"
            ;;
        0)
            echo "Beendet."
            exit 0
            ;;
        *)
            echo "Ungültige Auswahl."
            ;;
    esac
done
