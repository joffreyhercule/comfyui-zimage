#!/usr/bin/env sh
# Lance le studio (qui lance ComfyUI). Voir run.py pour la supervision.
set -e
cd "$(dirname "$0")"

if [ ! -x "venv/bin/python" ]; then
    echo "Le venv est absent : lancez d'abord ./install.sh"
    exit 1
fi
if [ ! -f "config.ini" ]; then
    echo "config.ini est absent : lancez d'abord ./install.sh"
    exit 1
fi

exec venv/bin/python run.py "$@"
