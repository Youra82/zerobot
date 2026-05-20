#!/bin/bash
# run_pipeline.sh — Vollständige zerobot Pipeline
# Discovery → Evolver → Backtest-Zusammenfassung
set -e

cd "$(dirname "$0")"

echo "=== zerobot Pipeline ==="
echo ""

# Discovery + Evolver
echo "[1/2] Quantum State Discovery..."
.venv/bin/python3 scan_and_learn.py "$@"

echo ""
echo "[2/2] Backtest-Zusammenfassung..."
.venv/bin/python3 run_backtest.py --capital 50

echo ""
echo "=== Pipeline abgeschlossen ==="
