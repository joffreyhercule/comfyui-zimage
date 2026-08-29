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
    echo "Python 3.10+ est introuvable. Installez-le, puis relancez ./install.sh"
    exit 1
fi

if [ ! -x "venv/bin/python" ]; then
    echo "Création du venv..."
    "$PY" -m venv venv
fi

echo "Installation des prérequis de l'installateur..."
venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
venv/bin/python -m pip install --disable-pip-version-check -q httpx

exec venv/bin/python install.py "$@"
