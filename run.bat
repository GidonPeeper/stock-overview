@echo off
rem Start Stock Overview on this computer and open it in your browser.
rem Reachable from this computer only: http://127.0.0.1:8000
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: setting up, about a minute...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist .venv\Scripts\python.exe (
    echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
    echo Tick "Add python.exe to PATH" in the installer, then run this again.
    pause
    exit /b 1
  )
)
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
echo Stock Overview: http://127.0.0.1:8000   (stop with Ctrl+C)
start "" http://127.0.0.1:8000
.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level warning
