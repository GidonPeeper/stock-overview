@echo off
rem Start Wimm on this computer and open it in your browser.
rem Reachable from this computer only: http://127.0.0.1:8000
rem   run.bat           start it
rem   run.bat update    get the newest version (your data folder is never touched)
cd /d "%~dp0"
if /i "%~1"=="update" goto update

:setup
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
if errorlevel 1 goto pipfailed
if /i "%~1"=="update" goto updated
echo Wimm: http://127.0.0.1:8000   (stop with Ctrl+C)
start "" http://127.0.0.1:8000
.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level warning
goto :eof

:pipfailed
echo.
echo Installing what the app needs failed: the message is above. Check the internet
echo connection and run this again. If it keeps failing, see TROUBLESHOOTING.md.
pause
exit /b 1

rem ---- run.bat update: git only changes the app's own files; data and .env stay as they are ----
:update
where git >nul 2>nul
if errorlevel 1 goto nogit
git rev-parse --git-dir >nul 2>nul
if errorlevel 1 goto norepo
set CHANGED=
for /f "delims=" %%i in ('git status --porcelain --untracked-files^=no') do set CHANGED=1
if defined CHANGED goto changed
for /f %%i in ('git rev-parse --short HEAD') do set BEFORE=%%i
echo Looking for a newer version...
git pull --ff-only --quiet
if errorlevel 1 goto pullfailed
for /f %%i in ('git rev-parse --short HEAD') do set AFTER=%%i
if "%BEFORE%"=="%AFTER%" echo Already up to date, version %AFTER%.
if not "%BEFORE%"=="%AFTER%" echo Updated from version %BEFORE% to %AFTER%.
goto setup

:updated
echo Done. If Wimm is running, close its window and start run.bat again.
exit /b 0

:nogit
echo Cannot update: the git program is not installed on this computer.
echo Install it from https://git-scm.com/downloads and run "run.bat update" again,
echo or download the newest version from https://github.com/GidonPeeper/Where-is-my-money
echo and copy your data folder, and .env if you have one, into the new folder.
pause
exit /b 1

:norepo
echo Cannot update: this folder was not downloaded with git, it has no .git folder.
echo Download the newest version from https://github.com/GidonPeeper/Where-is-my-money
echo and copy your data folder, and .env if you have one, into the new folder.
pause
exit /b 1

:changed
echo Cannot update: these files of the app were changed on this computer:
git status --short --untracked-files=no
echo If you did not change them on purpose, set the changes aside and update:
echo   git stash          - "git stash pop" brings them back
echo   run.bat update
echo Your data folder and .env are not affected either way.
pause
exit /b 1

:pullfailed
echo.
echo Could not update; nothing was changed. Git's message is above. Usually it is one of:
echo   - no internet connection: connect, then run "run.bat update" again;
echo   - "no tracking information": this copy is on another branch; run "git checkout main" first;
echo   - "Not possible to fast-forward": this copy has its own commits; ask whoever made them.
echo More help: TROUBLESHOOTING.md, "Updating".
pause
exit /b 1
