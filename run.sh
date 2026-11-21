#!/usr/bin/env bash
# Installe (si besoin) puis lance le serveur sur http://localhost:8000
# Si le serveur plante, il est relancé automatiquement (le bot reprend tout seul).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  .venv/bin/playwright install chromium
fi
echo "→ Ouvrez http://localhost:8000"
while true; do
  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 && break
  echo "Le serveur s'est arrêté (code $?). Redémarrage dans 10 s… (Ctrl+C pour quitter)"
  sleep 10
done
