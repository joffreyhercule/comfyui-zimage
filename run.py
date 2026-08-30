#!/usr/bin/env python3
"""Lance le studio, et ComfyUI avec lui.

C'est un **superviseur**, pas deux démarrages côte à côte : un Ctrl+C sur le studio
doit emporter ComfyUI, sinon une instance orpheline reste en mémoire avec 12 à 20 Go
de VRAM retenus, et le lancement suivant échoue sans qu'on comprenne pourquoi.

Trois précautions valent d'être signalées :
  - si ComfyUI répond déjà sur son port, on ne démarre rien et on ne tue rien —
    l'instance appartient à quelqu'un d'autre ;
  - le port est 8288, jamais 8188 : une installation personnelle n'est pas touchée ;
  - uvicorn tourne sans `reload`, qui respawnerait le parent et orphelinerait
    l'enfant à chaque sauvegarde de fichier.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from studio.comfy.layout import resolve_comfy_layout  # noqa: E402
from studio.config import (  # noqa: E402
    AGENT_PORT,
    COMFY_EXTRA_ARGS,
    COMFY_MANAGED,
    COMFY_PORT,
    COMFY_PORTABLE_DIR,
    COMFYUI_URL,
    LOGS_DIR,
)
from studio.ports import FORBIDDEN, FREE, bind_status  # noqa: E402

COMFY_LOG = LOGS_DIR / "comfyui.log"
STARTUP_TIMEOUT = 180.0
IS_WINDOWS = os.name == "nt"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def say(message: str) -> None:
    print(message, flush=True)


def port_ready(port: int, label: str, section: str) -> bool:
    """Le port répond-il de nous ? Message clair et arrêt net sinon.

    Sondé AVANT de démarrer quoi que ce soit : sans cela ComfyUI charge ses modèles
    une minute durant, uvicorn échoue sur son bind, et tout est défait sans qu'on ait
    rien appris de plus qu'une trace d'erreur au milieu du journal.
    """
    status = bind_status(port)
    if status == FREE:
        return True
    if status == FORBIDDEN:
        # WinError 10013 : une plage réservée par Hyper-V, WSL ou un moteur de
        # conteneurs. Le port ne marchera jamais, quoi qu'on attende.
        say(f"Le port {port} ({label}) est refusé par le système, pas occupé.")
        say("C'est une plage réservée. Pour les lister, sous Windows :")
        say("    netsh interface ipv4 show excludedportrange protocol=tcp")
        say(f"Changez [{section}] port dans config.ini, ou relancez l'installateur :")
        say("il en choisira un qui tienne sur cette machine.")
    else:
        say(f"Le port {port} ({label}) est déjà pris par un autre programme.")
        say(f"Une instance tourne peut-être déjà : http://127.0.0.1:{port}")
        say(f"Sinon, changez [{section}] port dans config.ini.")
    return False


def comfy_responds(timeout: float = 2.0) -> bool:
    try:
        with urlopen(f"{COMFYUI_URL}/system_stats", timeout=timeout) as response:
            return response.status == 200
    except (URLError, OSError, ValueError):
        return False


def start_comfy() -> subprocess.Popen | None:
    """Démarre ComfyUI en processus enfant, sortie vers `logs/comfyui.log`."""
    layout = resolve_comfy_layout(COMFY_PORTABLE_DIR)
    if layout is None:
        say(f"ComfyUI est introuvable dans {COMFY_PORTABLE_DIR}.")
        say("Lancez l'installateur (install.bat / ./install.sh), ou corrigez")
        say("[comfyui] portable_dir dans config.ini.")
        return None
    python, main_py, cwd = layout
    if not port_ready(COMFY_PORT, "ComfyUI", "comfyui"):
        return None

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = open(COMFY_LOG, "w", encoding="utf-8", errors="replace")

    command = [
        str(python), "-s", str(main_py),
        "--port", str(COMFY_PORT),
        "--listen", "127.0.0.1",
        # Sans cela ComfyUI ouvrirait sa propre interface dans le navigateur ; c'est
        # le studio qu'on veut voir. (Et surtout pas --windows-standalone-build.)
        "--disable-auto-launch",
        *COMFY_EXTRA_ARGS,
    ]
    # `-s` isole le Python embarqué des site-packages de l'utilisateur : sans lui,
    # un paquet installé globalement peut masquer celui du portable.
    say(f"Démarrage de ComfyUI sur le port {COMFY_PORT} — journal : {COMFY_LOG}")

    if IS_WINDOWS:
        # Groupe de processus séparé : sinon le Ctrl+C de la console frapperait
        # l'enfant en même temps que nous, et l'arrêt propre ci-dessous n'aurait
        # plus personne à arrêter.
        creation = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(command, cwd=str(cwd), stdout=log_handle,
                                stderr=subprocess.STDOUT, creationflags=creation)
    return subprocess.Popen(command, cwd=str(cwd), stdout=log_handle,
                            stderr=subprocess.STDOUT, start_new_session=True)


def wait_for_comfy(process: subprocess.Popen | None) -> bool:
    """Sonde `/system_stats` chaque seconde. Le premier démarrage lit plusieurs Go."""
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if comfy_responds():
            return True
        if process is not None and process.poll() is not None:
            say(f"ComfyUI s'est arrêté immédiatement (code {process.returncode}).")
            say(f"Voir {COMFY_LOG}")
            return False
        time.sleep(1.0)
    say(f"ComfyUI n'a pas répondu en {STARTUP_TIMEOUT:.0f} s — voir {COMFY_LOG}")
    return False


def stop_comfy(process: subprocess.Popen | None) -> None:
    """Termine l'ARBRE de processus : ComfyUI lance des workers, et tuer le seul
    parent laisserait la VRAM occupée par des orphelins."""
    if process is None or process.poll() is not None:
        return
    say("Arrêt de ComfyUI…")
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"Arrêt de ComfyUI imparfait : {exc}")
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def open_browser_soon(url: str) -> None:
    """Ouvre le navigateur une fois uvicorn en écoute (thread, pour ne pas bloquer)."""
    def go() -> None:
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — pas de navigateur, pas de drame
            pass

    threading.Thread(target=go, daemon=True).start()


def main() -> int:
    # Le port du studio d'abord : c'est le seul échec qui, sans cela, se produirait
    # APRÈS une minute de chargement de modèles, pour tout défaire aussitôt.
    if not port_ready(AGENT_PORT, "studio", "server"):
        return 1

    comfy_process = None
    if comfy_responds():
        # Instance déjà en route : ni démarrée ni arrêtée par nous.
        say(f"ComfyUI répond déjà sur {COMFYUI_URL} — réutilisé tel quel.")
    elif COMFY_MANAGED:
        comfy_process = start_comfy()
        if comfy_process is None:
            return 1
        if not wait_for_comfy(comfy_process):
            stop_comfy(comfy_process)
            return 1
        say("ComfyUI est prêt.")
    else:
        say(f"[comfyui] managed = false et rien ne répond sur {COMFYUI_URL}.")
        say("Démarrez ComfyUI vous-même, puis relancez.")
        return 1

    url = f"http://127.0.0.1:{AGENT_PORT}"
    say(f"Studio : {url}")
    try:
        import uvicorn

        open_browser_soon(url)
        # reload=False : un reload respawnerait ce processus et orphelinerait
        # l'enfant ComfyUI à chaque sauvegarde de fichier.
        uvicorn.run("studio.main:app", host="127.0.0.1", port=AGENT_PORT,
                    reload=False, log_level="info")
    except KeyboardInterrupt:
        say("")
    finally:
        stop_comfy(comfy_process)
    return 0


if __name__ == "__main__":
    sys.exit(main())
