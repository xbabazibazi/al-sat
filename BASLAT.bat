@echo off
chcp 65001 >nul
title AL-SAT BOT - Baslatici
cd /d "%~dp0"

echo ============================================
echo   AL-SAT BOT baslatiliyor...
echo ============================================
echo.

REM Bot ve panel ayri pencerelerde acilir; kapatmak icin pencereleri kapatin.
start "AL-SAT BOT (bot)" cmd /k "chcp 65001 >nul && python -m src.main"
timeout /t 3 /nobreak >nul
start "AL-SAT BOT (panel)" cmd /k "chcp 65001 >nul && python -m src.panel"
timeout /t 3 /nobreak >nul

echo Bot ve panel calisiyor.
echo   Panel : http://localhost:8484
echo   Bildirim: Telegram @Veksorbot
echo.
echo Tarayici aciliyor...
start http://localhost:8484
echo.
echo Bu pencereyi kapatabilirsiniz - bot ve panel kendi pencerelerinde devam eder.
timeout /t 8 /nobreak >nul
