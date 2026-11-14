@echo off
setlocal
cd /d "%~dp0backend"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg and FFprobe are required. Install FFmpeg and add it to PATH.
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
findstr /R /C:"^APP_ENCRYPTION_KEY=..*" .env >nul
if errorlevel 1 (
  for /f %%K in ('python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"') do set APPKEY=%%K
  powershell -NoProfile -Command "$p='.env'; $s=Get-Content $p -Raw; if ($s -match '(?m)^APP_ENCRYPTION_KEY=.*$') { $s=[regex]::Replace($s,'(?m)^APP_ENCRYPTION_KEY=.*$','APP_ENCRYPTION_KEY=%APPKEY%') } else { $s += \"`r`nAPP_ENCRYPTION_KEY=%APPKEY%`r`n\" }; Set-Content -NoNewline $p $s"
  echo Generated an encryption key in backend\.env. Back up this file securely.
)
echo Starting ClipForge AI at http://127.0.0.1:8787
python -m uvicorn main:app --host 127.0.0.1 --port 8787
pause
