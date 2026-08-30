#!/usr/bin/env python3
"""Installateur de comfyui-zimage — Windows, Linux, macOS.

Séquentiel et **idempotent** : relancé après une coupure, il reprend où il s'était
arrêté. Un fichier déjà présent à la bonne taille est sauté, un téléchargement
interrompu reprend à l'octet où il en était, une installation ComfyUI existante est
réutilisée sans rien copier.

Il est appelé par `install.bat` / `install.sh` avec le Python du venv du projet, qui
n'a alors que httpx (et py7zr sous Windows) : l'étape 1 installe le reste.

Il parle les treize langues de l'interface. La langue du système est retenue
d'office ; le sélecteur d'ouverture ne fait que permettre d'en changer, et ne
s'affiche que sur un terminal interactif — `--lang` et `--yes` le sautent.

    python install.py [--lang CODE] [--variant quantized|bf16] [--comfy-dir CHEMIN]
                      [--install-ollama | --no-ollama] [--no-run] [--yes]
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
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from studio.comfy.layout import comfy_models_dir, resolve_comfy_layout  # noqa: E402
from studio.ports import pick_port  # noqa: E402
from studio.i18n import (  # noqa: E402
    LANGUAGES,
    NATIVE_NAMES,
    detect_system_language,
    load as load_language,
    normalize as normalize_language,
    t,
)


# ---------- Constantes ----------

PY_MIN = (3, 10)

# ComfyUI tourne sur un port dédié : une installation personnelle sur 8188 n'est
# ni touchée ni lancée.
COMFY_PORT = 8288
# 8388 et non 8000 : sous Windows, 8000 tombe régulièrement dans une plage réservée par
# Hyper-V ou WSL, où toute liaison est refusée (WinError 10013). 8188 est le ComfyUI de
# tout le monde, 8288 le nôtre, 8388 le studio.
STUDIO_PORT = 8388

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

# Le portable Windows n'existe qu'en build NVIDIA, et une installation source suit
# l'accélérateur détecté : hors CUDA, le torch en place n'a aucun GPU à qui parler.
# Sans `--cpu`, ComfyUI appelle `torch.cuda.current_device()` dès l'import de
# `model_management` et meurt en violation d'accès, avant même d'ouvrir son port.
# La plus ancienne architecture présente dans les wheels cu130, sm_75 (Turing,
# RTX 20xx et GTX 16xx) : `torch.cuda.get_arch_list()` du portable rend
# ['sm_75', 'sm_80', 'sm_86', 'sm_90', 'sm_100', 'sm_120']. CUDA 13 a laissé Pascal,
# Maxwell et Volta derrière lui, et `nvidia-smi` répond pourtant pour ces cartes : sans
# ce contrôle, l'installation aboutit et la première image échoue sur
# « no kernel image is available for execution on the device ».
CUDA_MIN_CAPABILITY = (7, 5)

# Marge au-dessus du poids des modèles : l'OS, Python, les activations. En dessous, la
# machine tient encore, en swappant — d'où un avertissement, pas un refus.
RAM_MARGIN = 4_000_000_000

CPU_ARGS = ("--cpu", "--disable-cuda-malloc")

# En mode CPU, ComfyUI refuse le fp16 comme le bf16 — `should_use_fp16` et
# `should_use_bf16` rendent False sur un device CPU — et charge donc l'UNet en
# fp32 : les 12,3 Go du bf16 en réclament ~24,6 en mémoire, d'un seul bloc, le
# mode CPU désactivant aussi le chargement partiel. En dessous de ce seuil,
# `--bf16-unet` force la moitié : plus lent, faute d'instructions bf16 sur un x86
# grand public, mais c'est la seule version qui tienne en mémoire.
CPU_FP32_MIN_RAM = 24_000_000_000
CPU_BF16_ARG = "--bf16-unet"

# Une image en 1024² se compte en dizaines de minutes sur un processeur : le
# garde-fou des 15 minutes couperait des jobs qui avancent normalement.
CPU_JOB_TIMEOUT = "3600"

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


def display_width(text: str) -> int:
    """Largeur d'un texte en colonnes de terminal, pas en points de code.

    `完了` occupe deux cellules par caractère. Compter avec `len()` ferait déborder
    chaque ligne réécrite sur place — barres de progression comprises, qui
    laisseraient alors une traînée à droite.
    """
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def clip(text: str, width: int) -> str:
    """Tronque à `width` colonnes, sans jamais couper au milieu d'une cellule."""
    total, kept = 0, []
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if total + step > width:
            break
        kept.append(char)
        total += step
    return "".join(kept)


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
    print("\r" + pad(clip(line, LINE_WIDTH), LINE_WIDTH), end="", flush=True)


def title(step: int, text: str) -> None:
    """Titre d'étape, avec l'avancement global : sur une installation qui dure une
    demi-heure, savoir qu'on en est à 2 sur 6 vaut mieux que de compter les titres."""
    global _current_step
    _current_step = step
    say("")
    head = f"=== [{step}/{TOTAL_STEPS}] {text} "
    rule = "=" * max(0, LINE_WIDTH - BAR_WIDTH - 10 - display_width(head))
    say(head + rule
        + f" {bar(step / TOTAL_STEPS, BAR_WIDTH)} {100 * step // TOTAL_STEPS:3d}%")


def ok(text: str) -> None:
    say(f"  + {text}")


def warn(text: str) -> None:
    say(f"  ! {text}")


def die(text: str) -> None:
    say("")
    say(t("abort", message=text))
    sys.exit(1)


def human(n: float) -> str:
    """Taille lisible. Unités décimales, comme celles annoncées par Hugging Face.

    L'unité et la virgule décimale viennent des chaînes : `1.5 GB` en anglais,
    `1,5 Go` en français, `1.5 ГБ` en russe.
    """
    units = (t("unit.b"), t("unit.kb"), t("unit.mb"), t("unit.gb"))
    for index, unit in enumerate(units):
        if abs(n) < 1000 or index == len(units) - 1:
            text = f"{n:.0f} {unit}" if index == 0 else f"{n:.1f} {unit}"
            return text.replace(".", t("format.decimal"))
        n /= 1000
    return f"{n:.1f} {units[-1]}"


def duration(seconds: float) -> str:
    """Durée courte : `42 s`, `3 min 07 s`, `1 h 12 min`.

    Le remplissage à deux chiffres est fait ici, pas dans le gabarit : une chaîne
    traduite n'a pas à porter de spécificateur de format que le traducteur pourrait
    casser.
    """
    seconds = int(max(0, seconds))
    if seconds < 60:
        return t("dur.s", s=seconds)
    if seconds < 3600:
        return t("dur.ms", m=seconds // 60, s=f"{seconds % 60:02d}")
    return t("dur.hm", h=seconds // 3600, m=f"{(seconds % 3600) // 60:02d}")


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
                print("\r" + pad(clip(f"    {frame} {self.label}   {elapsed}",
                                      LINE_WIDTH), LINE_WIDTH),
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
        die(t("comfy.remove_failed", path=path))


def ask_yes_no(question: str, default: bool, assume: bool | None = None) -> bool:
    """Question fermée. `assume` court-circuite (drapeaux non interactifs), et une
    entrée fermée (pipe, CI) retombe sur le défaut au lieu de lever EOFError."""
    if assume is not None:
        say(t("prompt.forced", question=question,
              answer=t("prompt.yes") if assume else t("prompt.no")))
        return assume
    suffix = t("prompt.suffix_yes") if default else t("prompt.suffix_no")
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except EOFError:
        return default
    # Chaque langue liste ses lettres d'accord, `y` compris : sur un clavier
    # quelconque, c'est la touche que tout le monde essaie.
    return answer[0] in t("prompt.yes_letters") if answer else default


def ask_text(question: str, skip: bool = False) -> str:
    if skip:
        return ""
    try:
        return input(f"{question} ").strip().strip('"').strip("'")
    except EOFError:
        return ""


# ---------- Langue ----------

def language_label(code: str) -> str:
    """Le nom natif si la console sait l'écrire, le nom anglais sinon.

    Une console restée en cp437 afficherait `???` en face de 3, 10 et 13 : autant
    proposer `Japanese` que trois points d'interrogation."""
    native = NATIVE_NAMES[code]
    return native if _encodable(native) else LANGUAGES[code]


def choose_language(default: str) -> str:
    """Sélecteur d'ouverture : les treize langues, celle du système présélectionnée.

    La détection décide, l'invite ne fait que laisser changer d'avis — d'où la
    validation par simple `Entrée`. On accepte le numéro comme le code, parce que
    quelqu'un qui cherche sa langue tape aussi bien `3` que `es`.
    """
    codes = list(NATIVE_NAMES)
    rows = -(-len(codes) // 3)  # trois colonnes, remplies verticalement
    width = max(display_width(language_label(code)) for code in codes) + 6

    say("")
    say(f"  {t('lang.title')}")
    for row in range(rows):
        cells = []
        for column in range(3):
            index = column * rows + row
            if index < len(codes):
                cells.append(pad(f"{index + 1:>2}. {language_label(codes[index])}", width))
        say("    " + "".join(cells).rstrip())

    prompt = t("lang.hint", count=len(codes), default=language_label(default))
    try:
        answer = input(f"  {prompt} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(codes):
        return codes[int(answer) - 1]
    return normalize_language(answer) or default


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


def nvidia_too_old() -> tuple[str, str] | None:
    """`(nom, capacité)` de la carte NVIDIA si aucune n'atteint `CUDA_MIN_CAPABILITY`.

    `nvidia-smi` répond pour tout ce que le pilote voit, y compris des cartes que le
    torch qu'on va installer ne sait pas piloter. Une capacité illisible — pilote trop
    ancien pour la requête, `N/A` — ne bloque rien : on ne renonce à une carte que sur
    une réponse claire. Et une seule carte utilisable suffit à garder le mode CUDA.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "--query-gpu=name,compute_cap", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None

    oldest = None
    for line in (done.stdout or "").splitlines():
        name, _, capability = line.partition(",")
        try:
            parsed = tuple(int(part) for part in capability.strip().split("."))
        except ValueError:
            continue
        if parsed >= CUDA_MIN_CAPABILITY:
            return None
        if oldest is None or parsed > oldest[1]:
            oldest = (name.strip(), parsed)
    return (oldest[0], ".".join(str(part) for part in oldest[1])) if oldest else None


def total_ram() -> int:
    """RAM physique en octets, ou 0 si la plateforme ne sait pas la dire.

    Sans psutil : l'installateur ne dépend que de la bibliothèque standard tant que
    l'étape 1 n'a pas posé les requirements, et cette mesure sert dès le préflight.
    """
    if IS_WINDOWS:
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except (AttributeError, OSError):
            pass
        return 0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return 0


def cpu_mode_args(accelerator: str) -> list[str]:
    """Les arguments que ComfyUI exige sur cette machine, ou une liste vide.

    Vide veut dire « le GPU répond » : CUDA partout, ROCm et MPS sur une installation
    source, qui a reçu le torch correspondant. Sous Windows il n'y a pas d'autre cas
    que CUDA — le portable est une build NVIDIA, et qu'un Radeon ou un Arc soit
    présent ne change rien à ce que son torch sait piloter.
    """
    gpu_ready = accelerator == "cuda" or (
        not IS_WINDOWS and accelerator in ("rocm", "mps"))
    if gpu_ready:
        return []
    arguments = list(CPU_ARGS)
    # RAM inconnue : on choisit le pire cas. Le bf16 ne coûte que du temps, le fp32
    # sur une machine trop juste coûte l'installation entière.
    if total_ram() < CPU_FP32_MIN_RAM:
        arguments.append(CPU_BF16_ARG)
    return arguments


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
        ok(t("pre.cuda_ok", cuda=cuda))
        return "quantized"
    warn(t("pre.cuda_old", cuda=cuda or t("pre.cuda_absent")))
    warn(t("pre.cuda_hint"))
    return "bf16"


def total_download(variant: str) -> int:
    models = MODEL_VARIANTS[variant]
    return models["unet"][1] + models["clip"][1] + MODEL_VAE[1]


def preflight(args) -> tuple[str, str]:
    title(0, t("step.preflight"))
    if sys.version_info < PY_MIN:
        die(t("pre.python_old", minimum=f"{PY_MIN[0]}.{PY_MIN[1]}",
              found=platform.python_version()))
    ok(t("pre.python", version=platform.python_version(), executable=sys.executable))
    ok(t("pre.platform", system=platform.system(), machine=platform.machine()))
    # Un Mac Intel n'a ni Metal utilisable par torch, ni build x86_64 publiée depuis la
    # 2.2 : l'installation ira au bout, le moteur peut très bien ne pas démarrer. On
    # prévient sans interdire — c'est la machine de quelqu'un, pas la nôtre.
    if sys.platform == "darwin" and platform.machine() != "arm64":
        warn(t("pre.mac_intel"))

    accelerator = detect_accelerator()
    # Une carte trop ancienne n'est pas une carte : la déclasser ici, et tout ce qui
    # suit — variante, index PyTorch, arguments de ComfyUI — suit sans cas particulier.
    if accelerator == "cuda":
        old_card = nvidia_too_old()
        if old_card:
            warn(t("pre.cuda_too_old", name=old_card[0], cap=old_card[1],
                   minimum=".".join(str(part) for part in CUDA_MIN_CAPABILITY)))
            accelerator = "cpu"

    variant = args.variant or variant_for(accelerator)
    ok(t("pre.accelerator", accelerator=accelerator, variant=t(f"variant.{variant}"))
       + (t("pre.forced") if args.variant else ""))
    if args.variant == "quantized" and accelerator != "cuda":
        warn(t("pre.variant_eager"))

    # Le dire ici, pas au récapitulatif : c'est avant vingt minutes de
    # téléchargement qu'on veut savoir que la machine générera sur son processeur.
    cpu_arguments = cpu_mode_args(accelerator)
    if cpu_arguments:
        warn(t("pre.cpu_mode", args=" ".join(cpu_arguments)))
        say("    " + t("pre.cpu_slow"))
        if CPU_BF16_ARG in cpu_arguments:
            ram = total_ram()
            say("    " + t("pre.cpu_bf16",
                           ram=human(ram) if ram else t("pre.cpu_ram_unknown")))
    else:
        # Sur GPU, les poids passent quand même par la RAM, et la mémoire unifiée d'un
        # Mac EST la RAM. Personne ne peut rien y faire ici : on prévient, c'est tout.
        ram, weights = total_ram(), total_download(variant)
        # Le message annonce le poids des modèles, la condition y ajoute la marge :
        # afficher la somme laisserait croire que les poids pèsent 4 Go de plus.
        if ram and ram < weights + RAM_MARGIN:
            warn(t("pre.ram_short", ram=human(ram), needed=human(weights)))

    # Modèles + ComfyUI (portable extrait ~10 Go, ou venv torch ~8 Go) + marge.
    needed = total_download(variant) + 12_000_000_000
    free = shutil.disk_usage(ROOT).free
    if free < needed:
        die(t("pre.disk_short", drive=ROOT.anchor, free=human(free), needed=human(needed)))
    ok(t("pre.disk", free=human(free), needed=human(needed)))
    return accelerator, variant


# ---------- 1. Dépendances du projet ----------

def install_requirements() -> None:
    title(1, t("step.deps"))
    req = ROOT / "requirements.txt"
    code = run_spinning([sys.executable, "-m", "pip", "install",
                         "--disable-pip-version-check", "-r", str(req)],
                        t("deps.label", file=req.name))
    if code != 0:
        die(t("deps.failed"))


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
        ok(t("dl.present", label=label, size=human(dest.stat().st_size)))
        return dest
    if dest.exists():
        warn(t("dl.bad_size", label=label, size=human(dest.stat().st_size)))
        dest.unlink()

    part = dest.with_name(dest.name + ".part")
    resume_from = part.stat().st_size if part.exists() else 0
    if expected_size and resume_from > expected_size:
        resume_from = 0
        part.unlink()
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    say("  > " + t("dl.start", label=label,
                   size=human(expected_size) if expected_size else t("dl.unknown_size"))
        + (t("dl.resume", size=human(resume_from)) if resume_from else ""))

    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                      timeout=timeout) as response:
        if resume_from and response.status_code == 200:
            # Range ignoré : la réponse repart de zéro, le fragment ne vaut plus rien.
            warn(t("dl.no_range"))
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
                           t("dl.progress", done=human(written), total=human(total),
                             speed=human(speed), remaining=duration(remaining)))
    clear_line()

    size = part.stat().st_size
    if expected_size and size != expected_size:
        part.unlink(missing_ok=True)
        die(t("dl.mismatch", label=label, got=human(size),
              expected=human(expected_size)))
    os.replace(part, dest)
    ok(t("dl.done", label=label, size=human(size)))
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
            download(SEVENZR_URL, local, label=t("comfy.7zr_label"))
        except Exception as exc:  # réseau, 404, proxy : py7zr prendra le relais
            warn(t("comfy.7zr_missing", error=exc))
            return None
    return str(local), [str(local), "x", "-y"]


def extract_7z(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tool = find_7z()
    if tool:
        exe, base = tool
        # -bsp1 envoie la progression sur stdout : c'est ce qui alimente la barre.
        code = run_with_percent(base + ["-bsp1", f"-o{dest}", str(archive)],
                                t("comfy.extracting", name=archive.name))
        if code == 0:
            ok(t("comfy.extracted", name=archive.name))
            return
        warn(t("comfy.extract_retry"))
    try:
        import py7zr
    except ImportError:
        die(t("comfy.no_extractor"))
    # py7zr n'expose pas de progression exploitable simplement : animation seule.
    with Spinner(t("comfy.extracting_py7zr", name=archive.name)):
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
        die(t("comfy.no_release"))
    ok(t("comfy.release", tag=meta.get("tag_name", "?"), asset=asset["name"]))

    archive = ROOT / asset["name"]
    download(asset["browser_download_url"], archive, asset.get("size", 0), asset["name"])

    staging = ROOT / "_comfy_extract"
    if staging.exists():
        with Spinner(t("comfy.clean_staging")):
            remove_tree(staging)
    extract_7z(archive, staging)

    # L'archive porte son propre dossier racine ; on remonte son contenu dans
    # comfyui/ pour que la disposition soit la même qu'après un git clone.
    inner = next((p for p in staging.iterdir() if p.is_dir()), staging)
    if target.exists():
        # Une installation précédente incomplète : la destination doit être libre,
        # sinon `rename` la prendrait pour un dossier d'accueil et y imbriquerait
        # l'arborescence (Windows refuse même carrément, en WinError 5).
        with Spinner(t("comfy.clean_target", name=target.name)):
            remove_tree(target)
    try:
        # Même volume : un rename est instantané et atomique. Le repli copie
        # 10 Go fichier par fichier — plusieurs minutes, d'où l'animation, et une
        # interruption au milieu y laisserait une arborescence à moitié remplie
        # (que la relance détecte comme invalide et refait proprement).
        os.rename(inner, target)
    except OSError as exc:
        warn(t("comfy.move_failed", error=exc.strerror or exc))
        with Spinner(t("comfy.copying", name=target.name)):
            shutil.copytree(inner, target, dirs_exist_ok=True)
    with Spinner(t("comfy.clean_temp")):
        remove_tree(staging)
    archive.unlink(missing_ok=True)
    # L'extracteur téléchargé n'a servi qu'ici : 600 ko qui n'ont pas à traîner à la
    # racine du projet. Une réinstallation le reprendra si besoin.
    (ROOT / "7zr.exe").unlink(missing_ok=True)


def install_comfy_source(target: Path, accelerator: str) -> None:
    """Clone + venv + torch, pour Linux et macOS où le portable n'existe pas."""
    if not shutil.which("git"):
        die(t("comfy.no_git"))
    if not target.exists():
        if run_spinning(["git", "clone", "--depth", "1", COMFY_GIT, str(target)],
                        t("comfy.clone_label", url=COMFY_GIT)) != 0:
            die(t("comfy.clone_failed"))
    else:
        ok(t("comfy.cloned"))

    venv = target / "venv"
    if not venv.exists():
        if run_spinning([sys.executable, "-m", "venv", str(venv)],
                        t("comfy.venv_label")) != 0:
            die(t("comfy.venv_failed"))
    python = venv / "bin" / "python"
    if not python.exists():
        python = venv / "Scripts" / "python.exe"

    index = TORCH_INDEX.get(accelerator)
    torch_cmd = [str(python), "-m", "pip", "install", "torch", "torchvision", "torchaudio"]
    if index:
        torch_cmd += ["--index-url", index]
    if run_spinning(torch_cmd, t("comfy.torch_label", accelerator=accelerator)) != 0:
        die(t("comfy.torch_failed"))

    if run_spinning([str(python), "-m", "pip", "install", "-r",
                     str(target / "requirements.txt")],
                    t("comfy.reqs_label")) != 0:
        die(t("comfy.reqs_failed"))


def setup_comfy(args, accelerator: str, existing_ini: str) -> tuple[Path, Path]:
    """Rend `(racine ComfyUI, dossier models)`. N'installe que si rien n'est trouvé."""
    title(2, t("step.comfy"))
    for candidate in comfy_candidates(args, existing_ini):
        if resolve_comfy_layout(candidate):
            ok(t("comfy.reused", path=candidate))
            say("    " + t("comfy.reused_note"))
            return candidate, comfy_models_dir(candidate)

    target = (Path(args.comfy_dir).expanduser().resolve()
              if args.comfy_dir else ROOT / "comfyui")
    if not args.yes:
        say("  " + t("comfy.none_found"))
        answer = ask_text("  " + t("comfy.ask_path"))
        if answer:
            candidate = Path(answer).expanduser().resolve()
            if not resolve_comfy_layout(candidate):
                die(t("comfy.invalid", path=candidate))
            ok(t("comfy.reused", path=candidate))
            return candidate, comfy_models_dir(candidate)

    say("  " + t("comfy.installing", path=target))
    if IS_WINDOWS:
        install_comfy_windows(target)
    else:
        install_comfy_source(target, accelerator)

    if not resolve_comfy_layout(target):
        die(t("comfy.still_invalid", path=target))
    ok(t("comfy.installed", path=target))
    return target, comfy_models_dir(target)


# ---------- 3. Modèles ----------

def download_models(models_dir: Path, variant: str) -> dict[str, str]:
    title(3, t("step.models", variant=variant))
    files = dict(MODEL_VARIANTS[variant])
    files["vae"] = MODEL_VAE
    say("  " + t("models.destination", path=models_dir))
    say("  " + t("models.total", size=human(total_download(variant))))

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
    say("  > " + t("ollama.starting"))
    try:
        subprocess.Popen([binary, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        warn(t("ollama.start_failed", error=exc))
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
            warn(t("ollama.winget_failed"))
        setup = ROOT / "OllamaSetup.exe"
        download("https://ollama.com/download/OllamaSetup.exe", setup,
                 label="OllamaSetup.exe")
        say("  > " + t("ollama.gui"))
        subprocess.run([str(setup)])
        setup.unlink(missing_ok=True)
        return True

    if sys.platform == "darwin":
        if shutil.which("brew"):
            say("  > brew install --cask ollama")
            if subprocess.run(["brew", "install", "--cask", "ollama"]).returncode == 0:
                subprocess.run(["open", "-a", "Ollama"])
                return True
            warn(t("ollama.brew_failed"))
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
    warn(t("ollama.sudo"))
    done = subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True)
    if done.returncode == 0:
        return True
    warn(t("ollama.script_failed"))
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
                    warn(t("ollama.pull_refused", error=event["error"]))
                    return False
                done_bytes, total = event.get("completed", 0), event.get("total", 0)
                now = time.monotonic()
                if total and now - last_print >= 0.1:
                    last_print = now
                    # Le statut vient d'Ollama, en anglais : c'est un état de
                    # protocole (`pulling`, `verifying`), pas un message à nous.
                    render(event.get("status", t("ollama.pull_status"))[:34],
                           done_bytes / total, f"{human(done_bytes)}/{human(total)}")
    except Exception as exc:
        clear_line()
        warn(t("ollama.pull_failed", error=exc))
        return False
    clear_line()
    ok(t("ollama.ready", model=TRANSLATE_MODEL))
    return True


def setup_ollama(args) -> bool:
    """Rend `enabled` pour config.ini. N'est jamais bloquant : sans Ollama le studio
    fonctionne, les prompts partent simplement verbatim."""
    title(4, t("step.ollama"))

    if ollama_running():
        ok(t("ollama.answering"))
        return pull_translate_model()

    binary = ollama_binary()
    if binary:
        ok(t("ollama.stopped", path=binary))
        if start_ollama(binary):
            return pull_translate_model()
        warn(t("ollama.unreachable"))
        return False

    if args.no_ollama:
        say("  " + t("ollama.skipped"))
        return False

    for line in ("ollama.pitch1", "ollama.pitch2", "ollama.pitch3"):
        say("  " + t(line))
    if not IS_WINDOWS and sys.platform != "darwin":
        say("  " + t("ollama.linux_sudo"))
    if not ask_yes_no("  " + t("ollama.ask"), default=True,
                      assume=True if args.install_ollama else None):
        say("  " + t("ollama.declined"))
        return False

    if not run_ollama_installer():
        return False
    if not wait_for_ollama(60):
        binary = ollama_binary()
        if not (binary and start_ollama(binary)):
            warn(t("ollama.no_service"))
            warn(t("ollama.rerun"))
            return False
    return pull_translate_model()


# ---------- 5. config.ini ----------

def read_existing_ini() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH, encoding="utf-8")
    return parser


def answers(url: str) -> bool:
    """Quelque chose répond-il déjà, et de nous, sur cette URL ?"""
    try:
        return httpx.get(url, timeout=2.0).status_code == 200
    except (httpx.HTTPError, OSError, ValueError):  # rien n'écoute, ou pas en HTTP
        return False


def previous_port(previous: configparser.ConfigParser, section: str, default: int) -> int:
    """Le port de l'installation précédente : la clé `port`, sinon celui de son URL.

    Les config.ini d'avant ne déclaraient que l'URL. Une réinstallation doit retrouver
    le port qu'elle portait, sans quoi elle remettrait le défaut du projet sur une
    machine où l'utilisateur avait déjà tranché.
    """
    if previous.has_option(section, "port"):
        try:
            return previous.getint(section, "port")
        except ValueError:
            pass
    raw = (previous.get(section, "url", fallback="")
           or previous.get(section, "public_base_url", fallback="")).strip()
    try:
        return urlsplit(raw).port or default
    except ValueError:
        return default


def choose_port(previous: configparser.ConfigParser, section: str, default: int,
                health: str, label: str) -> int:
    """Le port à écrire : celui qu'on veut, ou le premier qui tienne sur cette machine.

    Un port occupé par le composant à qui il est destiné reste le bon — un ComfyUI déjà
    lancé, le studio en train de tourner pendant qu'on réinstalle. Un port refusé par le
    système, lui, ne marchera jamais : le découvrir ici évite de le découvrir au premier
    lancement, une fois les vingt gigaoctets téléchargés.
    """
    wanted = previous_port(previous, section, default)
    chosen = pick_port(wanted, is_mine=lambda port: answers(f"http://127.0.0.1:{port}{health}"))
    if chosen != wanted:
        warn(t("config.port_moved", name=label, old=wanted, new=chosen))
    return chosen


def keep_if_remote(value: str) -> str:
    """L'URL si elle désigne une autre machine, une chaîne vide sinon.

    Une URL locale écrite en dur redéclare le port et finira par le contredire : on la
    laisse tomber pour que le numéro reste la seule déclaration. Une URL distante, elle,
    dit quelque chose qu'un port ne dit pas.
    """
    value = (value or "").strip()
    if not value:
        return ""
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return ""
    return "" if host in (None, "127.0.0.1", "localhost", "::1", "0.0.0.0") else value


def write_config(comfy_root: Path, models: dict[str, str], ollama_enabled: bool,
                 previous: configparser.ConfigParser,
                 accelerator: str) -> tuple[int, int]:
    title(5, t("step.config"))
    layout = resolve_comfy_layout(comfy_root)
    output_dir = layout[2] / "output"

    def portable(path: Path) -> str:
        """Chemin relatif quand il est sous le projet, absolu sinon.

        Un ComfyUI installé dans `comfyui/` suit le dossier : renommer ou déplacer
        le projet ne demande alors aucune retouche de `config.ini`. Une installation
        extérieure, elle, reste désignée en absolu — elle ne bouge pas avec nous.
        """
        try:
            return path.relative_to(ROOT).as_posix()
        except ValueError:
            return str(path)

    # Un réglage machine posé à la main (--reserve-vram sur une petite carte) n'a pas
    # à être effacé par une réinstallation ; à défaut, ce que réclame l'accélérateur
    # détecté — sans quoi une machine sans CUDA écrirait une configuration que
    # ComfyUI ne sait pas démarrer, et l'erreur n'apparaîtrait qu'au premier run.
    cpu_arguments = cpu_mode_args(accelerator)
    extra_args = (previous.get("comfyui", "extra_args", fallback="").strip()
                  or " ".join(cpu_arguments))

    comfy_port = choose_port(previous, "comfyui", COMFY_PORT, "/system_stats", "ComfyUI")
    studio_port = choose_port(previous, "server", STUDIO_PORT, "/api/config", "studio")

    parser = configparser.ConfigParser()
    parser["comfyui"] = {
        "port": str(comfy_port),
        # Vide sauf pour un ComfyUI sur une autre machine : le port suffit à le joindre,
        # et deux déclarations d'un même numéro finissent toujours par diverger.
        "url": keep_if_remote(previous.get("comfyui", "url", fallback="")),
        "portable_dir": portable(comfy_root),
        "output_dir": portable(output_dir),
        "managed": "true",
        "extra_args": extra_args,
        "job_timeout": previous.get(
            "comfyui", "job_timeout",
            fallback=CPU_JOB_TIMEOUT if cpu_arguments else "900"),
    }
    parser["server"] = {
        "port": str(studio_port),
        # Vide = l'adresse locale du port ci-dessus. Ne se remplit que derrière un proxy,
        # ou pour une machine joignable d'ailleurs : c'est l'URL que le MCP rend.
        "public_base_url": keep_if_remote(previous.get("server", "public_base_url",
                                                       fallback="")),
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
    ok(t("config.written", name=CONFIG_PATH.name))
    if extra_args:
        say("  " + t("config.extra_args", args=extra_args))
    return comfy_port, studio_port


# ---------- 6. Récapitulatif ----------

def summary(comfy_root: Path, models_dir: Path, variant: str, models: dict[str, str],
            ollama_enabled: bool, ports: tuple[int, int]) -> None:
    title(6, t("step.done"))
    labels = [t("done.comfy"), t("done.models"), t("done.translation")]
    # La colonne est calée sur le plus long des trois libellés traduits : « Übersetzung »
    # est deux fois plus long que « 翻译 », et un ljust fixe casserait l'alignement.
    width = max(display_width(label) for label in labels) + 2
    say("  " + pad(labels[0], width) + str(comfy_root))
    say("  " + pad(labels[1], width)
        + f"{models_dir}  ({variant}, {human(total_download(variant))})")
    for kind in ("unet", "clip", "vae"):
        say("  " + " " * width + f"{kind:<5} {models[kind]}")
    say("  " + pad(labels[2], width)
        + t("done.translation_on" if ollama_enabled else "done.translation_off"))
    if not ollama_enabled:
        say("  " + " " * width + t("done.add_later"))
    say("")
    say("  " + t("done.to_start"))
    say("      run.bat" if IS_WINDOWS else "      ./run.sh")
    say("  " + t("done.listen", studio=ports[1], comfy=ports[0]))


# ---------- Démarrage ----------
# Pas une septième étape : rien à installer ici, et la barre s'arrête à 6/6.

def launch_studio(args) -> int:
    """Enchaîne sur `run.py`, dans la même console.

    L'installation finie, il ne reste rien à décider : la seule suite utile est de
    lancer le studio, et qui vient de regarder une barre de progression pendant
    vingt minutes n'a pas à aller chercher un second fichier pour voir le résultat.
    `--no-run` rend la main à qui installe sans vouloir démarrer (image, CI).
    """
    if args.no_run:
        return 0
    say("")
    if not ask_yes_no("  " + t("run.ask"), default=True,
                      assume=True if args.yes else None):
        return 0
    say("")
    try:
        process = subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=str(ROOT))
    except OSError as exc:
        # run.py effacé, interpréteur illisible : l'installation, elle, a réussi.
        warn(t("run.failed", error=exc))
        return 0
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # Le Ctrl+C de la console frappe les deux processus. run.py a son propre
            # arrêt propre à mener — il doit tuer l'arbre ComfyUI — et rendre la main
            # avant lui laisserait ces processus vivants derrière nous.
            continue


# ---------- Entrée ----------

def prescan_language(argv) -> str:
    """La langue, avant même qu'argparse existe.

    L'aide d'argparse est traduite elle aussi ; il faut donc connaître la langue
    pour construire le parseur, alors que `--lang` se trouve dans ce qu'il n'a pas
    encore lu. D'où cette lecture au plus simple : elle ne valide rien, `--lang`
    reste vérifié par `choices` juste après.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    for index, argument in enumerate(arguments):
        if argument == "--lang" and index + 1 < len(arguments):
            return normalize_language(arguments[index + 1]) or detect_system_language()
        if argument.startswith("--lang="):
            return normalize_language(argument.partition("=")[2]) or detect_system_language()
    return detect_system_language()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=t("cli.description"))
    parser.add_argument("--lang", choices=tuple(LANGUAGES),
                        help=t("cli.lang", codes=", ".join(LANGUAGES)))
    parser.add_argument("--variant", choices=("quantized", "bf16"),
                        help=t("cli.variant"))
    parser.add_argument("--comfy-dir", help=t("cli.comfy_dir"))
    parser.add_argument("--install-ollama", action="store_true",
                        help=t("cli.install_ollama"))
    parser.add_argument("--no-ollama", action="store_true", help=t("cli.no_ollama"))
    parser.add_argument("--no-run", action="store_true", help=t("cli.no_run"))
    parser.add_argument("--yes", "-y", action="store_true", help=t("cli.yes"))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    language = load_language(prescan_language(argv))
    args = parse_args(argv)

    # La langue du système fait foi ; le sélecteur ne sert qu'à en changer. On ne
    # le montre donc que si quelqu'un est là pour répondre, et si la ligne de
    # commande n'a pas déjà tranché.
    if not args.lang and not args.yes and sys.stdin is not None and sys.stdin.isatty():
        load_language(choose_language(language))

    say("")
    say(t("boot.header"))
    say(t("boot.project", path=ROOT))

    previous = read_existing_ini()
    accelerator, variant = preflight(args)
    install_requirements()

    comfy_root, models_dir = setup_comfy(
        args, accelerator, previous.get("comfyui", "portable_dir", fallback=""))
    layout = resolve_comfy_layout(comfy_root)
    variant = refine_variant(layout[0], variant, forced=bool(args.variant))

    models = download_models(models_dir, variant)
    ollama_enabled = setup_ollama(args)
    ports = write_config(comfy_root, models, ollama_enabled, previous, accelerator)
    summary(comfy_root, models_dir, variant, models, ollama_enabled, ports)
    return launch_studio(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("")
        say(t("interrupted"))
        sys.exit(130)
