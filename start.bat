@echo off
setlocal enabledelayedexpansion
title JMeter Agent Quick Start

echo ==========================================
echo   JMeter Agent - AI-Powered Performance Testing
echo ==========================================
echo.

set "BASE_DIR=%~dp0"
set "BASE_DIR=%BASE_DIR:~0,-1%"

:: Allow overriding the backend port via the PORT environment variable
if "%PORT%"=="" set "PORT=8000"

echo Checking backend port %PORT% availability...
powershell -Command "exit (Get-NetTCPConnection -LocalPort %PORT% -ErrorAction SilentlyContinue | Measure-Object).Count" >nul 2>&1
if %errorlevel% gtr 0 (
    echo ERROR: Port %PORT% is already in use.
    echo.
    echo Possible solutions:
    echo   1. Stop the existing service on port %PORT%.
    echo   2. Use a different port by running:
    echo        set PORT=8001
    echo        start.bat
    echo.
    pause
    exit /b 1
)

echo [1/2] Starting backend service on port %PORT%...
cd /d "%BASE_DIR%\jmeter-agent-backend"
for /f "delims=" %%u in ('python -m app.core.local_access') do set "ACCESS_URL=%%u"
start "JMeter Agent Backend" cmd /k "python -m app.main"

echo [2/2] Starting frontend dev server...
cd /d "%BASE_DIR%\jmeter-agent-frontend"
start "JMeter Agent Frontend" cmd /k "npm run dev"

echo.
echo ==========================================
echo Services started successfully!
echo Open:     !ACCESS_URL!
echo Backend:  http://localhost:%PORT%
echo API Docs: http://localhost:%PORT%/api/docs
echo ==========================================
echo.
timeout /t 5 /nobreak >nul
start "" "!ACCESS_URL!"
echo.
echo Note: Closing this window will NOT stop the services.
echo       Please close the backend/frontend windows manually.
echo.
pause
