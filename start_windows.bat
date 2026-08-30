@echo off
setlocal
cd /d "%~dp0"

echo Prado Watch - Windows dashboard
echo Discord alerts are disabled here to avoid duplicates with the Raspberry Pi.
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found.
    echo Install Python 3 from https://www.python.org/downloads/ and enable "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment...
    py -3 -m venv .venv
    if errorlevel 1 goto :failed
)

echo Checking Python packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet -r requirements.txt
if errorlevel 1 goto :failed

set "DISCORD_WEBHOOK_URL="
set "PRADO_DB=%CD%\prado_stock_windows.db"
set "PRADO_HOST=127.0.0.1"
set "PRADO_PORT=8080"

echo.
echo Opening at http://127.0.0.1:8080
start "" "http://127.0.0.1:8080"
".venv\Scripts\python.exe" app.py <nul
goto :eof

:failed
echo.
echo Setup failed. Review the error above.
pause
exit /b 1
