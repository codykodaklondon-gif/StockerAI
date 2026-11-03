@echo off
rem Stocker AI launcher for Windows. Double-click to start. First run installs dependencies (~2 min).
cd /d "%~dp0"
title Stocker AI
echo === Stocker AI ===

set "PY="
where py >nul 2>nul && (py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3")
if not defined PY (
  where python >nul 2>nul && (python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python")
)
if not defined PY (
  echo Python 3.10 or newer is required.
  echo Download it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during install.
  start https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\streamlit.exe" (
  echo First run: setting up ^(this takes a couple of minutes^)...
  %PY% -m venv .venv || (echo Setup failed. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
)
rem install on first run, and again whenever a new version adds requirements
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
  echo Installing components ^(needs internet^)...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q || (echo Install failed - check your internet connection. & pause & exit /b 1)
  copy /y requirements.txt .venv\requirements.installed >nul
)

echo Starting... your browser will open automatically
echo Keep this window open while using Stocker AI. Close it to stop.
".venv\Scripts\streamlit.exe" run app.py --server.headless false
pause
