@echo off
setlocal enabledelayedexpansion
REM Double-click this file in Windows Explorer to launch the dashboard.
REM Sets up the venv on first run (auto-downloads dependencies), then starts
REM the server. This window stays open on both success and failure so
REM errors are never hidden by the window auto-closing.

cd /d "%~dp0backend"

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: python was not found on PATH.
  echo Install Python 3.10+ from https://python.org and make sure to check
  echo "Add python.exe to PATH" during install, then run this again.
  goto :fail
)

for /f "delims=" %%v in ('python --version 2^>^&1') do echo Using %%v

if not exist ".venv" (
  echo Setting up Python environment ^(first run only^)...
  python -m venv .venv
  if errorlevel 1 (
    echo ERROR: Could not create the virtual environment.
    goto :fail
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  if errorlevel 1 (
    echo ERROR: Could not upgrade pip. Check your internet connection.
    goto :fail
  )
  ".venv\Scripts\pip.exe" install -r requirements.txt
  if errorlevel 1 (
    echo ERROR: Dependency install failed - see the pip output above for which package broke.
    goto :fail
  )
  echo Dependencies installed.
)

if not exist ".env" (
  echo No .env found - creating one with a fresh encryption key.
  copy /y ".env.example" ".env" >nul
  ".venv\Scripts\python.exe" -c "import re; content = open('.env').read(); from cryptography.fernet import Fernet; key = Fernet.generate_key().decode(); content = re.sub(r'(?m)^APP_SECRET_KEY=.*$', 'APP_SECRET_KEY=' + key, content); open('.env', 'w').write(content)"
  if errorlevel 1 (
    echo ERROR: Could not generate the encryption key for .env.
    goto :fail
  )
  echo.
  echo IMPORTANT: Edit backend\.env now and set DASHBOARD_PASSWORD to something
  echo private ^(not the default "change-me"^) - the app will refuse to start
  echo until you do. Then run this script again.
  echo.
  pause
  exit /b 0
)

REM Read HOST/PORT from .env if set, default to loopback-only for safety.
set "HOST=127.0.0.1"
set "PORT=8787"
for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
  if "%%a"=="HOST" if not "%%b"=="" set "HOST=%%b"
  if "%%a"=="PORT" if not "%%b"=="" set "PORT=%%b"
)

netstat -an | findstr /r /c:"[:.]%PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo ERROR: Port %PORT% is already in use - the dashboard may already be
  echo running at http://127.0.0.1:%PORT%, or another app is using that port.
  goto :fail
)

if "%HOST%"=="0.0.0.0" (
  echo WARNING: HOST=0.0.0.0 in .env - this dashboard will be reachable from
  echo outside this machine over plain HTTP ^(no TLS^). Only do this behind a
  echo firewall/VPN you trust - it holds your broker credentials.
)

echo Starting dashboard at http://%HOST%:%PORT% ...
REM Delay the browser open slightly so it doesn't hit the page before
REM uvicorn is actually listening.
start "" cmd /c "timeout /t 2 >nul & start "" http://127.0.0.1:%PORT%"

".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT%
if errorlevel 1 (
  echo ERROR: The dashboard server exited with an error - see the output above.
  goto :fail
)

goto :end

:fail
echo.
pause
exit /b 1

:end
echo.
pause
