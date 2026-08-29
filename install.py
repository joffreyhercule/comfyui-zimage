#!/usr/bin/env python3
"""Installateur de comfyui-zimage — Windows, Linux, macOS.

Séquentiel et **idempotent** : relancé après une coupure, il reprend où il s'était
arrêté. Un fichier déjà présent à la bonne taille est sauté, un téléchargement
interrompu reprend à l'octet où il en était, une installation ComfyUI existante est
réutilisée sans rien copier.

Il est appelé par `install.bat` / `install.sh` avec le Python du venv du projet, qui
n'a alors que httpx (et py7zr sous Windows) : l'étape 1 installe le reste.

    python install.py [--variant quantized|bf16] [--comfy-dir CHEMIN]
                      [--install-ollama | --no-ollama] [--yes]
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from studio.comfy.layout import comfy_models_dir, resolve_comfy_layout  # noqa: E402


# ---------- Constantes ----------

PY_MIN = (3, 10)

# ComfyUI tourne sur un port dédié : une installation personnelle sur 8188 n'est
# ni touchée ni lancée.
COMFY_PORT = 8288
STUDIO_PORT = 8000

HF_BASE = "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/"

# Deux variantes, choisies d'après l'accélérateur. Les kernels INT8 de ComfyUI
# passent par comfy-kitchen, qui désactive son backend hors CUDA >= 13 ; sur MPS
# l'int8 retombe sur le CPU et devient plus lent que le bf16, et il n'y a pas non
# plus de kernels fp8 pour le text encoder.
# Les tailles sont celles du dépôt : somme de contrôle pauvre mais suffisante —
# un octet manquant et le fichier est retéléchargé plutôt que chargé de travers.
MODEL_VARIANTS = {
    "quantized": {
        "unet": ("diffusion_models/z_image_turbo_int8_convrot.safetensors", 6_201_001_296),
        "clip": ("text_encoders/qwen_3_4b_fp8_mixed.safetensors", 5_631_994_051),
    },
    "bf16": {
        "unet": ("diffusion_models/z_image_turbo_bf16.safetensors", 12_309_866_400),
        "clip": ("text_encoders/qwen_3_4b.safetensors", 8_044_982_048),
    },
}
MODEL_VAE = ("vae/ae.safetensors", 335_304_388)

GITHUB_LATEST = "https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest"
PORTABLE_ASSET = "ComfyUI_windows_portable_nvidia.7z"
SEVENZR_URL = "https://www.7-zip.org/a/7zr.exe"
COMFY_GIT = "https://github.com/comfyanonymous/ComfyUI"

# Index PyTorch par accélérateur, pour l'installation source (Unix). cu130 est le
# seuil sous lequel comfy-kitchen désactive son backend CUDA : en dessous, l'int8
# ne servirait à rien.
TORCH_INDEX = {
    "cuda": "https://download.pytorch.org/whl/cu130",
    "rocm": "https://download.pytorch.org/whl/rocm6.4",
    "cpu": "https://download.pytorch.org/whl/cpu",
    "mps": None,  # les wheels par défaut embarquent MPS
}

OLLAMA_URL = "http://127.0.0.1:11434"
TRANSLATE_MODEL = "translategemma:latest"
OLLAMA_PATHS = (
    r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",
    "/Applications/Ollama.app/Contents/Resources/ollama",
    "/usr/local/bin/ollama",
    "~/.local/bin/ollama",
)

IS_WINDOWS = os.name == "nt"
CONFIG_PATH = ROOT / "config.ini"


# ---------- Console ----------
# Une console Windows en cp1252 planterait sur le premier accent ; on force l'UTF-8
# et on remplace ce qui ne passe pas plutôt que d'interrompre une installation de
# 20 Go sur un caractère.

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _encodable(text: str) -> bool:
    """La console sait-elle afficher ces caractères ? Un `chcp` oublié ne doit pas
    transformer une barre de progression en suite de points d'interrogation."""
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return True
    except (LookupError, UnicodeEncodeError):
        return False


BAR_FILL, BAR_EMPTY = ("█", "░") if _encodable("█░") else ("#", "-")
SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if _encodable("⠋") else "|/-\\"
BAR_WIDTH = 26
# Une ligne aussi large que la console, moins une colonne : écrire jusqu'au bord
# provoque un retour à la ligne parasite, et la barre laisserait une traînée.
LINE_WIDTH = max(78, min(shutil.get_terminal_size((110, 24)).columns - 1, 150))

TOTAL_STEPS = 6
_current_step = 0


def say(msg: str = "") -> None:
    print(msg, flush=True)


def bar(fraction: float, width: int = BAR_WIDTH) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * width))
    return f"[{BAR_FILL * filled}{BAR_EMPTY * (width - filled)}]"


def clear_line() -> None:
    print("\r" + " " * LINE_WIDTH + "\r", end="", flush=True)


def render(label: str, fraction: float, detail: str = "") -> None:
    """Une ligne de progression réécrite sur place. L'appelant limite la cadence :
    à 60 rafraîchissements par seconde, l'affichage coûterait plus que la copie."""
    line = f"    {bar(fraction)} {100 * fraction:5.1f}%  {label}"
    if detail:
        line += f"  {detail}"
    print("\r" + line[:LINE_WIDTH].ljust(LINE_WIDTH), end="", flush=True)


def title(step: int, text: str) -> None:
    """Titre d'étape, avec l'avancement global : sur une installation qui dure une
    demi-heure, savoir qu'on en est à 2 sur 6 vaut mieux que de compter les titres."""
    global _current_step
    _current_step = step
    say("")
    head = f"=== [{step}/{TOTAL_STEPS}] {text} "
    say(head.ljust(LINE_WIDTH - BAR_WIDTH - 10, "=")
        + f" {bar(step / TOTAL_STEPS, BAR_WIDTH)} {100 * step // TOTAL_STEPS:3d}%")


def ok(text: str) -> None:
    say(f"  + {text}")


def warn(text: str) -> None:
    say(f"  ! {text}")


def die(text: str) -> None:
    say("")
    say(f"ARRET : {text}")
    sys.exit(1)


def human(n: float) -> str:
    """Taille lisible. Unités décimales, comme celles annoncées par Hugging Face."""
    for unit in ("o", "ko", "Mo", "Go"):
        if abs(n) < 1000 or unit == "Go":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} Go"


def duration(seconds: float) -> str:
    """Durée courte : `42 s`, `3 min 07 s`, `1 h 12 min`."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {(seconds % 3600) // 60:02d} min"


class Spinner:
    """Animation pour les étapes dont on ne connaît pas l'avancement : pip, git,
    l'installation de PyTorch. Le temps écoulé est affiché, sans quoi rien ne
    distingue une compilation de dix minutes d'un processus figé."""

    def __init__(self, label: str):
        self.label = label
        self._stop = None
        self._thread = None
        self.started = 0.0

    def __enter__(self) -> "Spinner":
        import threading

        self.started = time.monotonic()
        self._stop = threading.Event()

        def spin() -> None:
            index = 0
            while not self._stop.wait(0.12):
                frame = SPIN_FRAMES[index % len(SPIN_FRAMES)]
                elapsed = duration(time.monotonic() - self.started)
                print(f"\r    {frame} {self.label}   {elapsed}".ljust(LINE_WIDTH),
                      end="", flush=True)
                index += 1

        self._thread = threading.Thread(target=spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        clear_line()
        if exc[0] is None:
            ok(f"{self.label} — {duration(time.monotonic() - self.started)}")


def run_spinning(cmd: list[str], label: str, **kwargs) -> int:
    """Sous-processus silencieux, sous animation. Sa sortie n'est montrée qu'en cas
    d'échec : réussie, elle noierait la progression sous des pages de logs pip."""
    with Spinner(label):
        done = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", **kwargs)
    if done.returncode != 0:
        say((done.stdout or "")[-2000:])
        say((done.stderr or "")[-2000:])
    return done.returncode


def run_with_percent(cmd: list[str], label: str) -> int:
    """Sous-processus qui crache des `NN%` sur sa sortie (7-Zip en `-bsp1`) : on les
    relaie dans une vraie barre. Les pourcentages arrivent sans retour à la ligne,
    d'où la lecture par petits blocs plutôt que ligne à ligne."""
    import re

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0)
    except OSError:
        return 1
    buffer, last_print = b"", 0.0
    started = time.monotonic()
    while True:
        chunk = process.stdout.read(80)
        if not chunk:
            break
        buffer = (buffer + chunk)[-320:]
        found = re.findall(rb"(\d{1,3})%", buffer)
        now = time.monotonic()
        if found and now - last_print >= 0.1:
            last_print = now
            render(label, int(found[-1]) / 100, duration(now - started))
    process.wait()
    clear_line()
    return process.returncode


def remove_tree(path: Path) -> None:
    """Suppression récursive qui survit aux fichiers en lecture seule.

    ComfyUI embarque son propre dépôt git, et les `.git/objects/pack/*.idx` sont
    posés en lecture seule : sous Windows `os.unlink` les refuse tant que l'attribut
    n'a pas été retiré. Un `rmtree(ignore_errors=True)` échouerait donc à moitié et
    laisserait derrière lui un dossier que le `rename` suivant prendrait pour une
    destination occupée — c'est exactement ce qui casse une réinstallation.
    """
    import stat

    if not path.exists():
        return

    def force(func, failed_path, _exc):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:  # `onexc` n'existe pas avant 3.12
        shutil.rmtree(path, onerror=lambda f, p, e: force(f, p, e))
    if path.exists():
        die(f"impossible de supprimer {path} — un programme le tient peut-être "
            "ouvert (explorateur, antivirus, ComfyUI en cours). Fermez-le et relancez.")


def ask_yes_no(question: str, default: bool, assume: bool | None = None) -> bool:
    """Question fermée. `assume` court-circuite (drapeaux non interactifs), et une
    entrée fermée (pipe, CI) retombe sur le défaut au lieu de lever EOFError."""
    if assume is not None:
        say(f"{question} {'oui' if assume else 'non'} (imposé en ligne de commande)")
        return assume
    suffix = "[O/n]" if default else "[o/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except EOFError:
        return default
    return answer[0] in ("o", "y") if answer else default


def ask_text(question: str, skip: bool = False) -> str:
    if skip:
        return ""
    try:
        return input(f"{question} ").strip().strip('"').strip("'")
    except EOFError:
        return ""


# ---------- 0. Préflight ----------

def detect_accelerator() -> str:
    """`cuda`, `rocm`, `mps` ou `cpu`, d'après ce qui répond sur la machine.

    On interroge les outils du pilote, pas torch : à ce stade torch n'est pas
    forcément installé, et c'est justement ce choix qui décidera de sa variante.
    """
    if sys.platform == "darwin":
        return "mps" if platform.machine() == "arm64" else "cpu"
    for tool, accel in (("nvidia-smi", "cuda"), ("rocminfo", "rocm")):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            if subprocess.run([exe], capture_output=True, timeout=30).returncode == 0:
                return accel
        except (OSError, subprocess.SubprocessError):
            continue
    return "cpu"


def variant_for(accelerator: str) -> str:
    """Seul CUDA a un backend int8 utilisable ; partout ailleurs le bf16 gagne."""
    return "quantized" if accelerator == "cuda" else "bf16"


def refine_variant(python: Path, variant: str, forced: bool) -> str:
    """Vérifie sur le torch réellement installé que l'int8 a bien un backend.

    `nvidia-smi` dit qu'il y a une carte, pas que torch est compilé pour CUDA >= 13 :
    un portable ancien ou un venv cu126 ferait tomber comfy-kitchen sur son chemin
    désactivé, et les 6,2 Go d'int8 seraient téléchargés pour rien.
    """
    if forced or variant != "quantized":
        return variant
    code = "import torch; print(torch.version.cuda or '')"
    try:
        done = subprocess.run([str(python), "-s", "-c", code],
                              capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return variant
    lines = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
    cuda = lines[-1] if lines else ""
    major = int(cuda.split(".")[0]) if cuda[:1].isdigit() else 0
    if major >= 13:
        ok(f"torch CUDA {cuda} — variante quantifiée confirmée")
        return "quantized"
    warn(f"torch CUDA « {cuda or 'absent'} » < 13 : pas de backend int8, bascule en bf16")
    warn("--variant quantized force la main si votre pile sait faire de l'int8")
    return "bf16"


def total_download(variant: str) -> int:
    models = MODEL_VARIANTS[variant]
    return models["unet"][1] + models["clip"][1] + MODEL_VAE[1]


def preflight(args) -> tuple[str, str]:
    title(0, "Préflight")
    if sys.version_info < PY_MIN:
        die(f"Python {PY_MIN[0]}.{PY_MIN[1]}+ requis, trouvé {platform.python_version()}")
    ok(f"Python {platform.python_version()} — {sys.executable}")
    ok(f"Plateforme {platform.system()} {platform.machine()}")

    accelerator = detect_accelerator()
    variant = args.variant or variant_for(accelerator)
    label = "quantifiée (int8/fp8)" if variant == "quantized" else "bf16 (pleine précision)"
    ok(f"Accélérateur {accelerator} → variante {label}"
       + (" [imposée]" if args.variant else ""))

    # Modèles + ComfyUI (portable extrait ~10 Go, ou venv torch ~8 Go) + marge.
    needed = total_download(variant) + 12_000_000_000
    free = shutil.disk_usage(ROOT).free
    if free < needed:
        die(f"espace libre insuffisant sur {ROOT.anchor} : {human(free)} disponibles, "
            f"{human(needed)} nécessaires")
    ok(f"Espace disque : {human(free)} libres, {human(needed)} nécessaires")
    return accelerator, variant


# ---------- 1. Dépendances du projet ----------

def install_requirements() -> None:
    title(1, "Dépendances Python du studio")
    req = ROOT / "requirements.txt"
    code = run_spinning([sys.executable, "-m", "pip", "install",
                         "--disable-pip-version-check", "-r", str(req)],
                        f"pip install -r {req.name}")
    if code != 0:
        die("pip install -r requirements.txt a échoué — voir les messages ci-dessus")


# ---------- Téléchargement ----------

def download(url: str, dest: Path, expected_size: int = 0, label: str = "") -> Path:
    """Télécharge `url` vers `dest`, avec reprise et vérification de taille.

    Trois propriétés font toute l'idempotence de l'installateur :
      - un `dest` déjà présent à la bonne taille n'est pas retéléchargé ;
      - un `.part` laissé par une coupure reprend en `Range: bytes=<n>-`, avec repli
        sur un téléchargement complet si le serveur répond 200 au lieu de 206 (tous
        les miroirs n'honorent pas Range, et concaténer une réponse complète à un
        fragment produirait un fichier corrompu de la bonne taille) ;
      - le nom final n'apparaît qu'après vérification, par `os.replace` : un fichier
        présent est donc toujours un fichier complet.
    """
    label = label or dest.name
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and (not expected_size or dest.stat().st_size == expected_size):
        ok(f"{label} déjà présent ({human(dest.stat().st_size)})")
        return dest
    if dest.exists():
        warn(f"{label} : taille inattendue ({human(dest.stat().st_size)}), retéléchargement")
        dest.unlink()

    part = dest.with_name(dest.name + ".part")
    resume_from = part.stat().st_size if part.exists() else 0
    if expected_size and resume_from > expected_size:
        resume_from = 0
        part.unlink()
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    say(f"  > {label} : {human(expected_size) if expected_size else 'taille inconnue'}"
        + (f", reprise à {human(resume_from)}" if resume_from else ""))

    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                      timeout=timeout) as response:
        if resume_from and response.status_code == 200:
            # Range ignoré : la réponse repart de zéro, le fragment ne vaut plus rien.
            warn("le serveur ignore Range, reprise impossible : on repart de zéro")
            resume_from = 0
        elif resume_from and response.status_code != 206:
            response.raise_for_status()
        else:
            response.raise_for_status()

        total = expected_size or (int(response.headers.get("content-length", 0)) + resume_from)
        mode = "ab" if resume_from else "wb"
        written = resume_from
        last_print, started = 0.0, time.monotonic()
        with open(part, mode) as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                if now - last_print >= 0.1:  # au plus 10 rafraîchissements par seconde
                    last_print = now
                    elapsed = max(0.001, now - started)
                    speed = (written - resume_from) / elapsed
                    remaining = (total - written) / speed if speed and total else 0
                    render(label, written / total if total else 0.0,
                           f"{human(written)}/{human(total)}  {human(speed)}/s  "
                           f"reste {duration(remaining)}")
    clear_line()

    size = part.stat().st_size
    if expected_size and size != expected_size:
        part.unlink(missing_ok=True)
        die(f"{label} : {human(size)} reçus au lieu de {human(expected_size)} — "
            "relancez l'installateur")
    os.replace(part, dest)
    ok(f"{label} téléchargé ({human(size)})")
    return dest


# ---------- 2. ComfyUI ----------

def comfy_candidates(args, existing_ini: str) -> list[Path]:
    """Emplacements testés, dans l'ordre : le plus explicite d'abord.

    `--comfy-dir` est exclusif : demander un emplacement précis, c'est refuser les
    autres. Sans lui, on descend la cascade jusqu'à l'invite.
    """
    if args.comfy_dir:
        return [Path(args.comfy_dir).expanduser().resolve()]
    candidates = [ROOT / "comfyui"]
    if existing_ini:
        candidates.append(Path(existing_ini).expanduser().resolve())
    home = Path.home()
    candidates += [home / "ComfyUI_windows_portable", home / "ComfyUI", ROOT.parent / "ComfyUI"]
    seen, unique = set(), []
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_7z() -> tuple[str, list[str]] | None:
    """`7z` du PATH, sinon `7zr.exe` téléchargé à côté. `py7zr` sert de dernier repli."""
    for name in ("7z", "7za", "7zr"):
        exe = shutil.which(name)
        if exe:
            return exe, [exe, "x", "-y"]
    local = ROOT / "7zr.exe"
    if not local.exists():
        try:
            download(SEVENZR_URL, local, label="7zr.exe (extracteur)")
        except Exception as exc:  # réseau, 404, proxy : py7zr prendra le relais
            warn(f"7zr.exe indisponible ({exc}) — extraction via py7zr")
            return None
    return str(local), [str(local), "x", "-y"]


def extract_7z(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tool = find_7z()
    if tool:
        exe, base = tool
        # -bsp1 envoie la progression sur stdout : c'est ce qui alimente la barre.
        code = run_with_percent(base + ["-bsp1", f"-o{dest}", str(archive)],
                                f"extraction de {archive.name}")
        if code == 0:
            ok(f"{archive.name} extrait")
            return
        warn("l'extracteur externe a échoué, tentative avec py7zr")
    try:
        import py7zr
    except ImportError:
        die("aucun extracteur 7z disponible : installez 7-Zip, ou `pip install py7zr`")
    # py7zr n'expose pas de progression exploitable simplement : animation seule.
    with Spinner(f"extraction de {archive.name} avec py7zr"):
        with py7zr.SevenZipFile(archive, mode="r") as archive_file:
            archive_file.extractall(path=dest)


def install_comfy_windows(target: Path) -> None:
    """Release portable NVIDIA : c'est la seule qui embarque un Python + torch prêts."""
    meta = httpx.get(GITHUB_LATEST, follow_redirects=True, timeout=60).json()
    asset = next((a for a in meta.get("assets", []) if a["name"] == PORTABLE_ASSET), None)
    if asset is None:
        asset = next((a for a in meta.get("assets", [])
                      if a["name"].endswith("_nvidia.7z")), None)
    if asset is None:
        die("aucune release portable NVIDIA trouvée sur GitHub — "
            "installez ComfyUI à la main et relancez avec --comfy-dir")
    ok(f"ComfyUI {meta.get('tag_name', '?')} — {asset['name']}")

    archive = ROOT / asset["name"]
    download(asset["browser_download_url"], archive, asset.get("size", 0), asset["name"])

    staging = ROOT / "_comfy_extract"
    if staging.exists():
        with Spinner("nettoyage d'une extraction précédente"):
            remove_tree(staging)
    extract_7z(archive, staging)

    # L'archive porte son propre dossier racine ; on remonte son contenu dans
    # comfyui/ pour que la disposition soit la même qu'après un git clone.
    inner = next((p for p in staging.iterdir() if p.is_dir()), staging)
    if target.exists():
        # Une installation précédente incomplète : la destination doit être libre,
        # sinon `rename` la prendrait pour un dossier d'accueil et y imbriquerait
        # l'arborescence (Windows refuse même carrément, en WinError 5).
        with Spinner(f"nettoyage de {target.name} (installation précédente incomplète)"):
            remove_tree(target)
    try:
        # Même volume : un rename est instantané et atomique. Le repli copie
        # 10 Go fichier par fichier — plusieurs minutes, d'où l'animation, et une
        # interruption au milieu y laisserait une arborescence à moitié remplie
        # (que la relance détecte comme invalide et refait proprement).
        os.rename(inner, target)
    except OSError as exc:
        warn(f"déplacement direct impossible ({exc.strerror or exc}), copie en cours")
        with Spinner(f"copie de ComfyUI vers {target.name}"):
            shutil.copytree(inner, target, dirs_exist_ok=True)
    with Spinner("nettoyage des fichiers temporaires"):
        remove_tree(staging)
    archive.unlink(missing_ok=True)
    # L'extracteur téléchargé n'a servi qu'ici : 600 ko qui n'ont pas à traîner à la
    # racine du projet. Une réinstallation le reprendra si besoin.
    (ROOT / "7zr.exe").unlink(missing_ok=True)


def install_comfy_source(target: Path, accelerator: str) -> None:
    """Clone + venv + torch, pour Linux et macOS où le portable n'existe pas."""
    if not shutil.which("git"):
        die("git est introuvable : installez-le, ou passez --comfy-dir sur une "
            "installation existante")
    if not target.exists():
        if run_spinning(["git", "clone", "--depth", "1", COMFY_GIT, str(target)],
                        f"git clone {COMFY_GIT}") != 0:
            die("le clone de ComfyUI a échoué")
    else:
        ok("dépôt ComfyUI déjà cloné")

    venv = target / "venv"
    if not venv.exists():
        if run_spinning([sys.executable, "-m", "venv", str(venv)],
                        "création du venv ComfyUI") != 0:
            die("la création du venv de ComfyUI a échoué")
    python = venv / "bin" / "python"
    if not python.exists():
        python = venv / "Scripts" / "python.exe"

    index = TORCH_INDEX.get(accelerator)
    torch_cmd = [str(python), "-m", "pip", "install", "torch", "torchvision", "torchaudio"]
    if index:
        torch_cmd += ["--index-url", index]
    if run_spinning(torch_cmd, f"installation de PyTorch ({accelerator}, plusieurs Go)") != 0:
        die("l'installation de PyTorch a échoué")

    if run_spinning([str(python), "-m", "pip", "install", "-r",
                     str(target / "requirements.txt")],
                    "installation des dépendances de ComfyUI") != 0:
        die("l'installation des dépendances de ComfyUI a échoué")


def setup_comfy(args, accelerator: str, existing_ini: str) -> tuple[Path, Path]:
    """Rend `(racine ComfyUI, dossier models)`. N'installe que si rien n'est trouvé."""
    title(2, "ComfyUI")
    for candidate in comfy_candidates(args, existing_ini):
        if resolve_comfy_layout(candidate):
            ok(f"installation existante réutilisée : {candidate}")
            say("    (rien n'est copié : les modèles iront dans son propre models/)")
            return candidate, comfy_models_dir(candidate)

    target = (Path(args.comfy_dir).expanduser().resolve()
              if args.comfy_dir else ROOT / "comfyui")
    if not args.yes:
        say("  Aucune installation ComfyUI trouvée.")
        answer = ask_text("  Chemin d'une installation existante, ou Entrée pour installer :")
        if answer:
            candidate = Path(answer).expanduser().resolve()
            if not resolve_comfy_layout(candidate):
                die(f"{candidate} ne contient ni main.py ni interpréteur ComfyUI")
            ok(f"installation existante réutilisée : {candidate}")
            return candidate, comfy_models_dir(candidate)

    say(f"  Installation de ComfyUI dans {target}")
    if IS_WINDOWS:
        install_comfy_windows(target)
    else:
        install_comfy_source(target, accelerator)

    if not resolve_comfy_layout(target):
        die(f"installation terminée mais {target} reste invalide (main.py ou "
            "interpréteur manquant)")
    ok(f"ComfyUI installé dans {target}")
    return target, comfy_models_dir(target)


# ---------- 3. Modèles ----------

def download_models(models_dir: Path, variant: str) -> dict[str, str]:
    title(3, f"Modèles Z-Image Turbo — variante {variant}")
    files = dict(MODEL_VARIANTS[variant])
    files["vae"] = MODEL_VAE
    say(f"  Destination : {models_dir}")
    say(f"  Total : {human(total_download(variant))} (déjà téléchargé = sauté)")

    names: dict[str, str] = {}
    for index, (kind, (relative, size)) in enumerate(files.items(), start=1):
        subdir, _, filename = relative.partition("/")
        download(HF_BASE + relative, models_dir / subdir / filename, size,
                 f"{filename} ({index}/{len(files)})")
        names[kind] = filename
    return names


# ---------- 4. Ollama ----------

def ollama_running() -> bool:
    """Le seul test qui compte : le service répond-il ? C'est celui que le studio
    refera au runtime, avant chaque traduction."""
    try:
        return httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3).status_code == 200
    except Exception:
        return False


def ollama_binary() -> str | None:
    """`which` d'abord, puis les emplacements usuels : après une installation, le
    PATH du shell courant n'a pas encore été rechargé."""
    found = shutil.which("ollama")
    if found:
        return found
    for raw in OLLAMA_PATHS:
        path = Path(os.path.expandvars(raw)).expanduser()
        if path.exists():
            return str(path)
    return None


def wait_for_ollama(seconds: int = 60) -> bool:
    """Le service met quelques secondes à monter ; enchaîner un pull trop tôt échoue
    pour rien."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ollama_running():
            return True
        time.sleep(2)
    return False


def start_ollama(binary: str) -> bool:
    say("  > démarrage du service Ollama")
    try:
        subprocess.Popen([binary, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        warn(f"impossible de démarrer Ollama : {exc}")
        return False
    return wait_for_ollama(30)


def run_ollama_installer() -> bool:
    """Voie native par plateforme, avec un repli sans droits élevés."""
    if IS_WINDOWS:
        if shutil.which("winget"):
            say("  > winget install Ollama.Ollama")
            done = subprocess.run(["winget", "install", "--id", "Ollama.Ollama", "-e",
                                   "--accept-package-agreements",
                                   "--accept-source-agreements"])
            if done.returncode == 0:
                return True
            warn("winget a échoué, repli sur l'installateur graphique")
        setup = ROOT / "OllamaSetup.exe"
        download("https://ollama.com/download/OllamaSetup.exe", setup,
                 label="OllamaSetup.exe")
        say("  > l'installateur graphique s'ouvre : terminez-le, puis revenez ici")
        subprocess.run([str(setup)])
        setup.unlink(missing_ok=True)
        return True

    if sys.platform == "darwin":
        if shutil.which("brew"):
            say("  > brew install --cask ollama")
            if subprocess.run(["brew", "install", "--cask", "ollama"]).returncode == 0:
                subprocess.run(["open", "-a", "Ollama"])
                return True
            warn("brew a échoué, repli sur le .dmg")
        dmg = ROOT / "Ollama.dmg"
        download("https://ollama.com/download/Ollama.dmg", dmg, label="Ollama.dmg")
        mount = "/Volumes/Ollama"
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-quiet", str(dmg)])
        try:
            app = Path(mount) / "Ollama.app"
            if app.exists():
                subprocess.run(["cp", "-R", str(app), "/Applications/"])
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", mount])
            dmg.unlink(missing_ok=True)
        # L'app crée elle-même le lien dans /usr/local/bin au premier lancement.
        subprocess.run(["open", "-a", "Ollama"])
        return True

    say("  > curl -fsSL https://ollama.com/install.sh | sh")
    warn("ce script demande sudo : votre mot de passe vous sera peut-être demandé")
    done = subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True)
    if done.returncode == 0:
        return True
    warn("le script officiel a échoué — installez Ollama à la main si vous le souhaitez")
    return False


def pull_translate_model() -> bool:
    """`translategemma` (3,3 Go), tiré par l'API HTTP plutôt que par le binaire.

    Le service vient d'être vérifié, donc l'API répond forcément — alors que le
    binaire, lui, peut être absent du PATH d'un shell ouvert avant l'installation.
    La progression arrive en JSON ligne à ligne.
    """
    say(f"  > ollama pull {TRANSLATE_MODEL}")
    last_print = 0.0
    try:
        with httpx.stream("POST", f"{OLLAMA_URL}/api/pull",
                          json={"model": TRANSLATE_MODEL},
                          timeout=httpx.Timeout(30.0, read=None)) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("error"):
                    clear_line()
                    warn(f"pull refusé : {event['error']}")
                    return False
                done_bytes, total = event.get("completed", 0), event.get("total", 0)
                now = time.monotonic()
                if total and now - last_print >= 0.1:
                    last_print = now
                    render(event.get("status", "téléchargement")[:34],
                           done_bytes / total, f"{human(done_bytes)}/{human(total)}")
    except Exception as exc:
        clear_line()
        warn(f"le téléchargement du modèle de traduction a échoué : {exc}")
        return False
    clear_line()
    ok(f"{TRANSLATE_MODEL} prêt")
    return True


def setup_ollama(args) -> bool:
    """Rend `enabled` pour config.ini. N'est jamais bloquant : sans Ollama le studio
    fonctionne, les prompts partent simplement verbatim."""
    title(4, "Ollama (traduction des prompts — optionnel)")

    if ollama_running():
        ok("Ollama répond déjà sur 11434")
        return pull_translate_model()

    binary = ollama_binary()
    if binary:
        ok(f"Ollama installé mais arrêté : {binary}")
        if start_ollama(binary):
            return pull_translate_model()
        warn("service injoignable — la traduction sera désactivée pour l'instant")
        return False

    if args.no_ollama:
        say("  Ollama ignoré (--no-ollama).")
        return False

    say("  Ollama est absent. Il sert à traduire vos prompts vers l'anglais avant")
    say("  de les envoyer au modèle. Sans lui, tout marche, mais un prompt écrit en")
    say(f"  français part tel quel. Coût : ~1 Go de logiciel + 3,3 Go de modèle.")
    if not IS_WINDOWS and sys.platform != "darwin":
        say("  Sur Linux, son installation demande sudo.")
    if not ask_yes_no("  L'installer maintenant ?", default=True,
                      assume=True if args.install_ollama else None):
        say("  Ollama non installé : les prompts partiront verbatim.")
        return False

    if not run_ollama_installer():
        return False
    if not wait_for_ollama(60):
        binary = ollama_binary()
        if not (binary and start_ollama(binary)):
            warn("Ollama installé mais le service ne répond pas encore")
            warn("relancez l'installateur plus tard : il rattrapera le modèle")
            return False
    return pull_translate_model()


# ---------- 5. config.ini ----------

def read_existing_ini() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH, encoding="utf-8")
    return parser


def write_config(comfy_root: Path, models: dict[str, str], ollama_enabled: bool,
                 previous: configparser.ConfigParser) -> None:
    title(5, "config.ini")
    layout = resolve_comfy_layout(comfy_root)
    output_dir = layout[2] / "output"

    parser = configparser.ConfigParser()
    parser["comfyui"] = {
        "url": f"http://127.0.0.1:{COMFY_PORT}",
        "portable_dir": str(comfy_root),
        "output_dir": str(output_dir),
        "managed": "true",
        # Préservé d'une installation précédente : c'est un réglage machine
        # (--reserve-vram sur une petite carte) que l'installateur n'a pas à effacer.
        "extra_args": previous.get("comfyui", "extra_args", fallback=""),
        "job_timeout": previous.get("comfyui", "job_timeout", fallback="900"),
    }
    parser["server"] = {
        "port": previous.get("server", "port", fallback=str(STUDIO_PORT)),
        "public_base_url": previous.get(
            "server", "public_base_url",
            fallback=f"http://127.0.0.1:{STUDIO_PORT}"),
    }
    parser["ollama"] = {
        "enabled": "true" if ollama_enabled else "false",
        "translate_model": TRANSLATE_MODEL,
        "url": OLLAMA_URL,
    }
    parser["models"] = {"unet": models["unet"], "clip": models["clip"], "vae": models["vae"]}
    parser["image"] = {
        "default_width": previous.get("image", "default_width", fallback="1024"),
        "default_height": previous.get("image", "default_height", fallback="1024"),
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        parser.write(handle)
    ok(f"{CONFIG_PATH.name} écrit")


# ---------- 6. Récapitulatif ----------

def summary(comfy_root: Path, models_dir: Path, variant: str, models: dict[str, str],
            ollama_enabled: bool) -> None:
    title(6, "Terminé")
    say(f"  ComfyUI    {comfy_root}")
    say(f"  Modèles    {models_dir}  ({variant}, {human(total_download(variant))})")
    for kind in ("unet", "clip", "vae"):
        say(f"             {kind:<5} {models[kind]}")
    say(f"  Traduction {'translategemma via Ollama' if ollama_enabled else 'désactivée — prompts verbatim'}")
    if not ollama_enabled:
        say("             (relancez install.py plus tard pour l'ajouter)")
    say("")
    say("  Pour démarrer :")
    say("      run.bat" if IS_WINDOWS else "      ./run.sh")
    say(f"  Le studio écoutera sur http://127.0.0.1:{STUDIO_PORT} "
        f"et ComfyUI sur {COMFY_PORT}.")


# ---------- Entrée ----------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Installateur de comfyui-zimage")
    parser.add_argument("--variant", choices=("quantized", "bf16"),
                        help="force la variante des modèles au lieu de la déduire")
    parser.add_argument("--comfy-dir",
                        help="racine d'une installation ComfyUI à réutiliser ou à créer")
    parser.add_argument("--install-ollama", action="store_true",
                        help="installe Ollama sans poser la question")
    parser.add_argument("--no-ollama", action="store_true",
                        help="n'installe pas Ollama et ne demande rien")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="accepte les valeurs par défaut sans rien demander")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    say("comfyui-zimage — installation")
    say(f"Projet : {ROOT}")

    previous = read_existing_ini()
    accelerator, variant = preflight(args)
    install_requirements()

    comfy_root, models_dir = setup_comfy(
        args, accelerator, previous.get("comfyui", "portable_dir", fallback=""))
    layout = resolve_comfy_layout(comfy_root)
    variant = refine_variant(layout[0], variant, forced=bool(args.variant))

    models = download_models(models_dir, variant)
    ollama_enabled = setup_ollama(args)
    write_config(comfy_root, models, ollama_enabled, previous)
    summary(comfy_root, models_dir, variant, models, ollama_enabled)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("")
        say("Interrompu. Relancez l'installateur : il reprendra où il s'est arrêté.")
        sys.exit(130)
