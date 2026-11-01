@echo off
setlocal enabledelayedexpansion
title JMeter Agent Quick Start

echo ==========================================
echo   JMeter Agent - AI-Powered Performance Testing
echo ==========================================
echo.

set "ROOT_DIR=%~dp0"
set "ROOT_DIR=%ROOT_DIR:~0,-1%"

:: Allow overriding the backend port via the PORT environment variable
if "%PORT%"=="" set "PORT=8000"
set "FRONTEND_PORT=3000"
set "BACKEND_URL=http://localhost:%PORT%/health"
set "FRONTEND_URL=http://localhost:%FRONTEND_PORT%/"

cd /d "%ROOT_DIR%\jmeter-agent-backend"
for /f "delims=" %%u in ('python -m app.core.local_access') do set "ACCESS_URL=%%u"
if "!ACCESS_URL!"=="" (
    echo ERROR: Could not read the access link. Check that Python and the backend dependencies are installed.
    echo.
    pause
    exit /b 1
)

:: A service that is already running is reused, so running this twice still opens the app.
call :backend_ready
if !errorlevel! equ 0 (
    echo [1/2] Backend is already running on port %PORT%.
) else (
    call :require_free_port %PORT%
    if !errorlevel! neq 0 exit /b 1
    echo [1/2] Starting backend service on port %PORT%...
    start "JMeter Agent Backend" cmd /k "python -m app.main"
)

call :frontend_ready
if !errorlevel! equ 0 (
    echo [2/2] Frontend is already running on port %FRONTEND_PORT%.
) else (
    call :require_free_port %FRONTEND_PORT%
    if !errorlevel! neq 0 exit /b 1
    echo [2/2] Starting frontend dev server...
    cd /d "%ROOT_DIR%\jmeter-agent-frontend"
    start "JMeter Agent Frontend" cmd /k "npm run dev -- --strictPort"
)

:: The link only works once both are up: it signs in through the backend, so opening it early ends at a prompt.
echo.
echo Waiting for the services to come up...
set /a TRIES=0
:wait_for_services
call :backend_ready
if !errorlevel! neq 0 goto not_ready
call :frontend_ready
if !errorlevel! equ 0 goto services_ready
:not_ready
set /a TRIES+=1
if !TRIES! geq 120 (
    echo ERROR: The services did not start within 2 minutes. Check the backend and frontend windows.
    echo.
    pause
    exit /b 1
)
ping -n 2 127.0.0.1 >nul
goto wait_for_services

:services_ready
echo.
echo ==========================================
echo Services are ready!
echo Open:     !ACCESS_URL!
echo Backend:  http://localhost:%PORT%
echo API Docs: http://localhost:%PORT%/api/docs
echo ==========================================
echo.
start "" "!ACCESS_URL!"
echo Note: Closing this window will NOT stop the services.
echo       Please close the backend/frontend windows manually.
echo.
pause
exit /b 0

:backend_ready
curl.exe -s -m 2 "%BACKEND_URL%" 2>nul | findstr /c:"healthy" >nul
exit /b %errorlevel%

:frontend_ready
curl.exe -s -m 2 "%FRONTEND_URL%" 2>nul | findstr /c:"JMeter Agent" >nul
exit /b %errorlevel%

:require_free_port
powershell -NoProfile -Command "exit (Get-NetTCPConnection -State Listen -LocalPort %1 -ErrorAction SilentlyContinue | Measure-Object).Count" >nul 2>&1
if %errorlevel% equ 0 exit /b 0
echo ERROR: Port %1 is already in use by another program.
echo.
echo Possible solutions:
echo   1. Stop the program that is using port %1.
if "%1"=="%PORT%" (
    echo   2. Use a different backend port by running:
    echo        set PORT=8001
    echo        start.bat
)
echo.
pause
exit /b 1
