@echo off
REM Lance le serveur ACCESSIBLE depuis le téléphone (même Wi-Fi), protégé par mot de passe.
REM Le bot tourne sur ce PC ; le téléphone sert de télécommande (application Android ou navigateur).
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
  .venv\Scripts\playwright install chromium
)
if "%ACCESS_PASSWORD%"=="" (
  echo Choisissez un mot de passe pour l'acces depuis le telephone.
  for /f "delims=" %%p in ('powershell -NoProfile -Command "$p = Read-Host -AsSecureString 'Mot de passe'; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p))"') do set "ACCESS_PASSWORD=%%p"
)
if "%ACCESS_PASSWORD%"=="" (
  echo Mot de passe vide : arret.
  pause
  exit /b 1
)
set BOT_HOST=0.0.0.0
echo.
echo ================================================================
echo  Dans l'application Android, entrez l'adresse ci-dessous
echo  (la ligne "Sur le telephone" apparait dans les logs au demarrage)
echo ================================================================
:loop
.venv\Scripts\uvicorn main:app --host 0.0.0.0 --port 8000
if %errorlevel% equ 0 goto :eof
echo.
echo Le serveur s'est arrete (code %errorlevel%). Redemarrage dans 10 secondes... (fermez cette fenetre pour quitter)
timeout /t 10 /nobreak >nul
goto loop
