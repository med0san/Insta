#!/usr/bin/env bash
# Lance le serveur ACCESSIBLE depuis le téléphone (même Wi-Fi), protégé par mot de passe.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
fi
if [ -z "$ACCESS_PASSWORD" ]; then
  read -r -s -p "Mot de passe pour l'accès depuis le téléphone : " ACCESS_PASSWORD; echo
fi
[ -z "$ACCESS_PASSWORD" ] && { echo "Mot de passe vide : arrêt."; exit 1; }
export ACCESS_PASSWORD BOT_HOST=0.0.0.0
while true; do
  .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 && break
  echo "Le serveur s'est arrêté (code $?). Redémarrage dans 10 s… (Ctrl+C pour quitter)"; sleep 10
done
