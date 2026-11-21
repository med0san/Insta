@echo off
REM Installe (si besoin) puis lance le serveur sur http://localhost:8000
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
  .venv\Scripts\playwright install chromium
)
if "%ANTHROPIC_API_KEY%"=="" echo ATTENTION : ANTHROPIC_API_KEY n'est pas defini, l'analyse IA echouera.
echo Ouvrez http://localhost:8000
.venv\Scripts\uvicorn main:app --host 127.0.0.1 --port 8000
