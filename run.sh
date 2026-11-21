#!/usr/bin/env bash
# Installe (si besoin) puis lance le serveur sur http://localhost:8000
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  .venv/bin/playwright install chromium
fi
if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "⚠  ANTHROPIC_API_KEY n'est pas défini : l'analyse IA échouera."
fi
echo "→ Ouvrez http://localhost:8000"
exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
