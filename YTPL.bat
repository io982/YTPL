@echo off
title YTPL - YouTube Playlist Converter

echo ==========================================
echo         YTPL - YouTube Playlist Converter
echo ==========================================
echo.

:: Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.8+ and add to PATH.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Run the script
python "%~dp0YTPL.py"
set EXITCODE=%errorlevel%

echo.
echo ==========================================
if %EXITCODE% equ 0 (
    echo Done! Press any key to exit.
) else (
    echo Script finished with errors ^(code %EXITCODE%^).
    echo Press any key to exit.
)
echo ==========================================
pause
