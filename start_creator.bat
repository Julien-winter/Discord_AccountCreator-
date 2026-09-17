@echo off
title PandaChecker Account Creator
cd /d "%~dp0"

echo.
echo   PandaChecker Account Creator
echo   ============================
echo.
echo   Mails:     data\mails.txt
echo   Proxies:   data\joiner_proxies.txt
echo   Output:    accounts.txt
echo.

set /p COUNT="Anzahl Accounts (Standard: 10): "
if "%COUNT%"=="" set COUNT=10

echo.
python create_accounts.py %COUNT%

echo.
echo Druecke eine Taste zum Beenden...
pause >nul
