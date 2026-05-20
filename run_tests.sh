#!/bin/bash
# run_tests.sh — Pytest-Suite für zerobot

cd "$(dirname "$0")"

echo "=== zerobot Tests ==="
.venv/bin/python3 -m pytest tests/ -v
