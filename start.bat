@echo off
chcp 65001 >nul
cd /d "%~dp0"

py -3 --version >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0start_app.py"
) else (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo Python 3 is required. Install Python and try again.
        pause
        exit /b 1
    )
    python "%~dp0start_app.py"
)

if errorlevel 1 pause
