@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Avoid picking up another Python or NumPy from the machine environment.
set "PYTHONPATH="
set "PYTHONHOME="

echo HydroBridge
echo Working folder: %CD%
echo.

set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"

if not defined PY (
  echo Python 3.11 or 3.12 was not found.
  echo Install 64-bit Python from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create .venv. Use 64-bit Python 3.11 or 3.12.
    pause
    exit /b 1
  )
)

echo Installing packages into .venv ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
if errorlevel 1 (
  echo Binary wheels were not available for every package. Trying a normal install...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install msvc-runtime
".venv\Scripts\python.exe" -c "import numpy, matplotlib, flask; print('Ready: numpy', numpy.__version__)"
if errorlevel 1 goto :fail

echo.
echo Starting HydroBridge at http://127.0.0.1:5050
echo Leave this window open. Close it to stop the app.
echo.
start "" "http://127.0.0.1:5050"
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Setup or startup failed. See the messages above.
pause
exit /b 1
