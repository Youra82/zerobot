#!/bin/bash
# ZeroBot Update-Skript
cp secret.json secret.json.bak
git fetch origin
git reset --hard origin/main
cp secret.json.bak secret.json
rm secret.json.bak
find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -delete
chmod +x *.sh
echo "ZeroBot aktualisiert."
