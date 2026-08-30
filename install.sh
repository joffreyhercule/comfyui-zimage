#!/usr/bin/env sh
# Bootstrap Linux / macOS : Python, venv, httpx, puis install.py fait tout le reste.
# Un shell ne sait ni afficher une progression, ni reprendre un téléchargement, ni
# parler à l'API GitHub : il s'arrête donc à la première ligne utile.
set -e
cd "$(dirname "$0")"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    # Le seul message qu'aucun Python ne peut traduire, faute de Python.
    echo "Python 3.10+ not found. Install it, then run ./install.sh again"
    echo "Python 3.10+ est introuvable. Installez-le, puis relancez ./install.sh"
    exit 1
fi

# Les trois messages du bootstrap, dans la langue du système. Le shell ne sait ni
# lire un JSON ni interpréter une locale : il délègue au module i18n, avec
# l'anglais en repli si le module manque ou se tait.
say() {
    message=$("$PY" -m studio.i18n "$1" 2>/dev/null) || message=""
    [ -n "$message" ] || message="$2"
    echo "$message"
}

if [ ! -x "venv/bin/python" ]; then
    say boot.venv "Creating the virtual environment..."
    "$PY" -m venv venv
fi

say boot.prereq "Installing the installer's prerequisites..."
venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
venv/bin/python -m pip install --disable-pip-version-check -q httpx

exec venv/bin/python install.py "$@"
