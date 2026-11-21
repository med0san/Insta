@echo off
REM Installe (si besoin) puis lance le serveur sur http://localhost:8000
REM Si le serveur plante, il est relancé automatiquement (le bot reprend tout seul).
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
  .venv\Scripts\playwright install chromium
)
echo Ouvrez http://localhost:8000
:loop
.venv\Scripts\uvicorn main:app --host 127.0.0.1 --port 8000
if %errorlevel% equ 0 goto :eof
echo.
echo Le serveur s'est arrete (code %errorlevel%). Redemarrage dans 10 secondes... (fermez cette fenetre pour quitter)
timeout /t 10 /nobreak >nul
goto loop
