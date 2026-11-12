@echo off
REM Double-click launcher for the Photos library reader.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3.10+ is required but was not found on PATH.
    echo Install it from https://www.python.org/downloads/ and try again.
    pause
    exit /b 1
  )
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create the virtual environment.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)

".venv\Scripts\python.exe" -m app.launcher %*
if errorlevel 1 pause
endlocal
