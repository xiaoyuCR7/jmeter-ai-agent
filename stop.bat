@echo off
setlocal enabledelayedexpansion
title JMeter Agent Stop

:: Stops what start.bat started: its two windows, and the services on their ports if they still answer.
:: A port held by some other program is reported and left alone.

if "%PORT%"=="" set "PORT=8000"
set "FRONTEND_PORT=3000"

echo Stopping JMeter Agent...
taskkill /FI "WINDOWTITLE eq JMeter Agent Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq JMeter Agent Frontend*" /T /F >nul 2>&1
:: Give the processes a moment to release their ports.
ping -n 2 127.0.0.1 >nul

call :stop_port %PORT% "http://localhost:%PORT%/health" "healthy" "Backend"
call :stop_port %FRONTEND_PORT% "http://localhost:%FRONTEND_PORT%/" "JMeter Agent" "Frontend"

echo.
echo Done.
if not "%1"=="/quiet" pause
exit /b 0

:stop_port
:: %1 port, %2 URL, %3 text the page shows when it is ours, %4 name
powershell -NoProfile -Command "exit (Get-NetTCPConnection -State Listen -LocalPort %1 -ErrorAction SilentlyContinue | Measure-Object).Count" >nul 2>&1
if %errorlevel% equ 0 (
    echo   %~4: stopped.
    exit /b 0
)
curl.exe -s -m 2 "%~2" 2>nul | findstr /c:"%~3" >nul
if %errorlevel% neq 0 (
    echo   %~4: port %1 is used by another program; left running.
    exit /b 0
)
for /f %%p in ('powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -LocalPort %1 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique"') do (
    taskkill /PID %%p /T /F >nul 2>&1
)
echo   %~4: stopped ^(port %1^).
exit /b 0
