@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM Bootstrap Windows : Python, venv, httpx, puis install.py fait tout le reste.
REM Un .bat ne sait ni afficher une progression, ni reprendre un telechargement,
REM ni parler a l'API GitHub : il s'arrete donc a la premiere ligne utile.

set "PY="
for %%P in ("py -3" "python") do (
    if not defined PY (
        %%~P -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>nul && set "PY=%%~P"
    )
)
if not defined PY (
    REM Le seul message qu'aucun Python ne peut traduire, faute de Python.
    echo Python 3.10+ not found. Install it from https://www.python.org/downloads/
    echo with "Add python.exe to PATH" ticked, then run install.bat again.
    echo Python 3.10+ est introuvable. Installez-le depuis https://www.python.org/downloads/
    echo en cochant "Add python.exe to PATH", puis relancez install.bat.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    call :say boot.venv "Creating the virtual environment..."
    %PY% -m venv venv || (call :say boot.venv_failed "Could not create the virtual environment." & pause & exit /b 1)
)

call :say boot.prereq "Installing the installer's prerequisites..."
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q httpx py7zr || (call :say boot.pip_failed "pip failed." & pause & exit /b 1)

"venv\Scripts\python.exe" install.py %*
set "CODE=%ERRORLEVEL%"
pause
exit /b %CODE%

REM Trois messages, dans la langue du systeme. Le .bat ne sait ni lire un JSON ni
REM deviner une locale : il delegue au module i18n, avec l'anglais en repli si le
REM module manque ou se tait.
:say
set "MSG=%~2"
for /f "usebackq delims=" %%M in (`%PY% -m studio.i18n %1 2^>nul`) do set "MSG=%%M"
echo %MSG%
exit /b 0
