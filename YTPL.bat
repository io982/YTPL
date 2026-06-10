@echo off
chcp 65001 >nul
title YTPL - YouTube Playlist Converter

echo ==========================================
echo         YTPL - YouTube Playlist Converter
echo ==========================================
echo.

:: Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python не найден. Установите Python 3.8+ и добавьте в PATH.
    echo Скачать: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Run the script
python "%~dp0YTPL.py"

echo.
echo ==========================================
echo Готово! Нажмите любую клавишу для выхода.
echo ==========================================
pause
