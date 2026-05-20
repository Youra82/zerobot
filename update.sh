#!/bin/bash
# update.sh — VPS Deployment Script für zerobot
set -e

echo "=== zerobot Update ==="

# secret.json sichern (nicht im Git)
if [ -f secret.json ]; then
    cp secret.json secret.json.bak
    echo "secret.json gesichert."
fi

# quantum.db NICHT löschen (Patterns bleiben erhalten!)
# artifacts/tracker/* auch nicht (offene Positions)

git fetch origin
git reset --hard origin/main

if [ -f secret.json.bak ]; then
    cp secret.json.bak secret.json
    rm secret.json.bak
fi

find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -delete
chmod +x *.sh

echo "=== Update abgeschlossen ==="
