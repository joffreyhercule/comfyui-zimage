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
    echo Python 3.10+ est introuvable. Installez-le depuis https://www.python.org/downloads/
    echo en cochant "Add python.exe to PATH", puis relancez install.bat.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Creation du venv...
    %PY% -m venv venv || (echo Echec de la creation du venv. & pause & exit /b 1)
)

echo Installation des prerequis de l'installateur...
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q httpx py7zr || (echo Echec pip. & pause & exit /b 1)

"venv\Scripts\python.exe" install.py %*
set "CODE=%ERRORLEVEL%"
pause
exit /b %CODE%
