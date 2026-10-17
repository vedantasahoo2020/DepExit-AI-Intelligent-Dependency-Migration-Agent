@echo off
rem Double-click to start the DepExit dashboard. First run sets everything up (2-3 minutes).
title DepExit
cd /d "%~dp0"
set "PATH=%PATH%;%ProgramFiles%\Git\cmd;%LOCALAPPDATA%\Programs\Git\cmd"

if not exist ".venv\Scripts\depexit.exe" (
  echo Setting up DepExit for the first time...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python was not found. Install Python 3.12 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install. Then double-click this file again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --timeout 60 --retries 10 -e .
  if errorlevel 1 (
    echo.
    echo Installing failed - check your internet connection and try again.
    pause
    exit /b 1
  )
)

where git >nul 2>nul || (
  echo.
  echo Git was not found. Install it from https://git-scm.com/download/win and double-click this file again.
  pause
  exit /b 1
)

echo Starting DepExit... your browser will open. Keep this window open while you use it.
".venv\Scripts\depexit.exe" web
pause
