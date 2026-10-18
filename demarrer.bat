@echo off
REM Lance la base, l'API et l'interface web, puis ouvre le navigateur.
cd /d "%~dp0"

echo [1/3] Demarrage de PostgreSQL (Docker Desktop doit etre lance)...
docker compose up -d db || (echo ERREUR : lancez Docker Desktop puis relancez ce script. & pause & exit /b 1)

if not exist frontend\dist\index.html (
  echo [2/3] Premiere compilation de l'interface React...
  pushd frontend
  if not exist node_modules call npm install
  call npm run build
  popd
) else (
  echo [2/3] Interface deja compilee.
)

echo [3/3] Demarrage du serveur sur http://localhost:8001 ...
start "" cmd /c "timeout /t 8 >nul & start http://localhost:8001"
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
.venv\Scripts\python -m uvicorn app.api:app --port 8001
