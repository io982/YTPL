@echo off
:: UTF-8 console code page: video titles with non-ASCII characters
:: would otherwise fail to print on Windows
chcp 65001 >nul
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

:: Run the script (command-line arguments are forwarded as they are,
:: e.g. YTPL.bat <url> --from-description)
python "%~dp0YTPL.py" %*
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
