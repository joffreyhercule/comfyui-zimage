@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Le venv est absent : lancez d'abord install.bat
    pause
    exit /b 1
)
if not exist "config.ini" (
    echo config.ini est absent : lancez d'abord install.bat
    pause
    exit /b 1
)

"venv\Scripts\python.exe" run.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
