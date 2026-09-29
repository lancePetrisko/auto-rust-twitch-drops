@echo off
setlocal
cd /d "%~dp0"
title Rust Drops Watcher

set "PY_VERSION=3.11.9"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
set "PY_INSTALLER=%TEMP%\python-%PY_VERSION%-amd64.exe"
set "LOCAL_PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
set "VPY=.venv\Scripts\python.exe"
set "PY_CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"

echo ============================================
echo   Rust Drops Watcher
echo ============================================
echo.

rem --- Reuse the project venv if it is healthy, otherwise rebuild it ---
if exist "%VPY%" "%VPY%" -c "import sys" >nul 2>&1 || rmdir /s /q .venv >nul 2>&1
if exist "%VPY%" goto :deps

rem --- Find Python 3.9+ (the version check also skips the fake Microsoft Store python.exe) ---
set "PY="
py -3 -c "%PY_CHECK%" >nul 2>&1 && set PY=py -3
if not defined PY python -c "%PY_CHECK%" >nul 2>&1 && set PY=python
if not defined PY if exist "%LOCAL_PY%" "%LOCAL_PY%" -c "%PY_CHECK%" >nul 2>&1 && set PY="%LOCAL_PY%"
if defined PY goto :make_venv

rem --- No Python found: install it for this user only (no admin needed) ---
echo Python was not found. Installing Python %PY_VERSION% for you...
echo This only happens once and can take a few minutes.
echo.
echo Downloading Python installer...
curl -fL --progress-bar -o "%PY_INSTALLER%" "%PY_URL%"
if errorlevel 1 powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%'"
if not exist "%PY_INSTALLER%" goto :install_failed

echo Installing Python quietly...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del "%PY_INSTALLER%" >nul 2>&1
rem PATH does not refresh in this window, so use the install location directly
if not exist "%LOCAL_PY%" goto :install_failed
set PY="%LOCAL_PY%"
echo Python installed.
echo.

:make_venv
echo Setting up a private Python environment in .venv ...
%PY% -m venv .venv
if not exist "%VPY%" (
    echo.
    echo ERROR: Could not create the Python environment.
    goto :fail
)

:deps
"%VPY%" -c "import requests, bs4" >nul 2>&1 && goto :extension
echo Installing required libraries...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Library install failed. Check your internet connection and try again.
    goto :fail
)
echo Libraries installed.
echo.

:extension
if exist ".extension_setup_done" goto :run
echo ============================================
echo   ONE-TIME SETUP: Chrome extension
echo ============================================
echo The watcher needs a small Chrome extension to read your drop progress.
echo.
echo   1. In the Chrome tab that opens (chrome://extensions),
echo      turn on "Developer mode" in the top right.
echo   2. Click "Load unpacked".
echo   3. Pick the "extension" folder. A window showing it is opening now.
echo.
echo If Chrome does not open, open Chrome and type chrome://extensions in the address bar.
echo.
start "" explorer "%~dp0extension"
set "HAS_CHROME="
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>&1 && set "HAS_CHROME=1"
reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>&1 && set "HAS_CHROME=1"
if defined HAS_CHROME start "" chrome "chrome://extensions"
echo When the extension shows up in Chrome, press any key to continue.
pause >nul
type nul > ".extension_setup_done"
echo.

:run
set "SCRIPT="
for /f "delims=" %%F in ('dir /b /o-n "autoRustDrops_v*.py" 2^>nul') do if not defined SCRIPT set "SCRIPT=%%F"
if not defined SCRIPT (
    echo ERROR: Could not find autoRustDrops_v*.py next to this file.
    goto :fail
)

echo Opening your Twitch drops inventory. Keep this tab open.
start "" "https://www.twitch.tv/drops/inventory"
echo Starting %SCRIPT% ... the dashboard opens in your browser once it is ready.
echo Close this window to stop the watcher.
echo.
"%VPY%" "%SCRIPT%"
if errorlevel 1 goto :fail
goto :eof

:install_failed
echo.
echo ERROR: Automatic Python install failed.
echo Install Python manually from:
echo   %PY_URL%
echo Tick "Add python.exe to PATH" in the installer, then run start.bat again.

:fail
echo.
pause
exit /b 1
