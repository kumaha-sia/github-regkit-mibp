@echo off
title Github RegKit Launcher
color 0A

echo =========================================
echo       GITHUB REGKIT - LAUNCHER MENU
echo =========================================
echo.
echo 1. Start Web Server (Web Console)
echo    (Akan menjalankan 'python -m web.server')
echo.
echo 2. Run Registration Bot (CLI - 1 Account)
echo    (Akan menjalankan 'python main.py signup -c 1 --headless false')
echo.
echo 3. Exit
echo.
echo =========================================
set /p choice="Pilih mode run (1/2/3): "

if "%choice%"=="1" (
    echo.
    echo [*] Memulai Web Server...
    python -m web.server
    pause
) else if "%choice%"=="2" (
    echo.
    echo [*] Memulai Bot Registrasi...
    python main.py signup -c 1 --headless false
    pause
) else (
    exit
)
