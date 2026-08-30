"""Configuration du studio : chemins, ComfyUI, Ollama, bornes des dimensions.

Précédence des valeurs, du plus fort au plus faible : `config.ini` (écrit par
l'installateur, propre à la machine) → variable d'environnement → défaut codé ici.
Le défaut suffit à démarrer sans aucun fichier de configuration, ce qui rend les
tests et un premier lancement possibles avant toute installation.
"""

import configparser
import os
from pathlib import Path
from urllib.parse import urlsplit


# ---------- Chemins ----------

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
MEDIA_DIR: Path = PROJECT_ROOT / "media"
DB_DIR: Path = PROJECT_ROOT / "db"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
WORKFLOWS_DIR: Path = PROJECT_ROOT / "workflows"
STATIC_DIR: Path = PROJECT_ROOT / "studio" / "static"
GALLERY_DB_PATH: Path = DB_DIR / "gallery.json"
# Verrou dédié, jamais la base elle-même : le studio et le serveur MCP sont deux
# processus, et poser un verrou sur le fichier qu'on réécrit invite l'un à tronquer
# ce que l'autre est en train de lire.
GALLERY_LOCK_PATH: Path = DB_DIR / "gallery.lock"


# ---------- Lecture de config.ini ----------

_INI_PATH = PROJECT_ROOT / "config.ini"
_INI = configparser.ConfigParser()
if _INI_PATH.exists():
    _INI.read(_INI_PATH, encoding="utf-8")


def _ini(section: str, key: str, default: str | None = None) -> str | None:
    """Valeur de config.ini si elle y est, sinon `default`."""
    if _INI.has_option(section, key):
        return _INI.get(section, key).strip()
    return default


def _ini_int(section: str, key: str, default: int) -> int:
    """Entier de config.ini, `default` si absent ou illisible : un config.ini
    fautif doit dégrader vers le défaut, pas empêcher le studio de démarrer."""
    raw = _ini(section, key)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _ini_bool(section: str, key: str, default: bool) -> bool:
    raw = _ini(section, key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "oui")


# ---------- ComfyUI ----------

def _path(raw: str | None, default: Path) -> Path:
    """Chemin de `config.ini`, absolu ou **relatif au projet**.

    Un chemin relatif suit le dossier : renommer ou déplacer le projet ne casse
    alors rien. L'installateur écrit du relatif quand ComfyUI vit dans le projet,
    et de l'absolu quand il pointe sur une installation extérieure — qui, elle, ne
    bouge pas avec lui.
    """
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _port_of(url: str, default: int) -> int:
    """Port d'une URL, `default` si elle n'en porte pas.

    `urlsplit` plutôt qu'un `rsplit(":")` : une barre finale, un chemin ou une URL sans
    port rendraient un numéro faux, et un numéro faux ici lance ComfyUI d'un côté
    pendant que le client parle de l'autre.
    """
    try:
        return urlsplit(url).port or default
    except ValueError:
        return default


# Le PORT est la déclaration ; l'URL n'existe que pour un ComfyUI sur une autre
# machine, et fait alors foi — c'est d'elle qu'on redéduit le port. Écrire les deux
# reviendrait à déclarer le port deux fois, donc à pouvoir se contredire.
# 8288, pas 8188 : une installation personnelle de ComfyUI n'est ni touchée ni lancée.
COMFY_PORT: int = _ini_int("comfyui", "port", int(os.getenv("COMFY_PORT", "8288")))
COMFYUI_URL: str = ((_ini("comfyui", "url", os.getenv("COMFYUI_URL", "")) or "").rstrip("/")
                    or f"http://127.0.0.1:{COMFY_PORT}")
COMFY_PORT = _port_of(COMFYUI_URL, COMFY_PORT)

# Racine de l'installation ComfyUI. `resolve_comfy_layout` en déduit l'interpréteur
# et le main.py, quelle que soit la disposition (portable Windows ou clone git).
COMFY_PORTABLE_DIR: Path = _path(
    _ini("comfyui", "portable_dir", os.getenv("COMFY_PORTABLE_DIR")),
    PROJECT_ROOT / "comfyui")
COMFY_OUTPUT_DIR: Path = _path(
    _ini("comfyui", "output_dir", os.getenv("COMFY_OUTPUT_DIR")),
    COMFY_PORTABLE_DIR / "ComfyUI" / "output")
# `false` quand ComfyUI est démarré à la main ou par un autre outil : run.py se
# contente alors d'attendre qu'il réponde, et ne tue rien en sortant.
COMFY_MANAGED: bool = _ini_bool("comfyui", "managed", True)
COMFY_EXTRA_ARGS: list[str] = (_ini("comfyui", "extra_args", "") or "").split()

# Garde-fou contre un job zombie, pas une contrainte de performance : le tout
# premier job charge 12 à 20 Go de poids depuis le disque, ce qui peut prendre
# plusieurs minutes avant que le premier événement de progression n'arrive.
COMFY_JOB_TIMEOUT: float = float(_ini_int("comfyui", "job_timeout", 900))


# ---------- Modèles ----------
# Écrits par l'installateur selon la variante retenue. Ils ne sont pas figés dans
# workflow_api.json : `build_workflow` les pose comme les autres entrées, donc
# changer de variante est une ligne de config.ini, pas un JSON à réécrire.

MODEL_UNET: str = _ini("models", "unet", "z_image_turbo_bf16.safetensors")
MODEL_CLIP: str = _ini("models", "clip", "qwen_3_4b.safetensors")
MODEL_VAE: str = _ini("models", "vae", "ae.safetensors")


# ---------- Serveur ----------

# 8388 et non 8000 : sous Windows, 8000 tombe régulièrement dans une plage réservée
# par Hyper-V ou WSL, où toute liaison est refusée (WinError 10013), quand il n'est pas
# simplement pris par un autre serveur de développement. 8188 est le ComfyUI de tout le
# monde, 8288 le nôtre, 8388 le studio.
AGENT_PORT: int = _ini_int("server", "port", int(os.getenv("AGENT_PORT", "8388")))
# Vide = l'adresse locale du port ci-dessus, seule et unique déclaration. On ne la
# remplit que derrière un proxy ou pour une machine joignable d'ailleurs : c'est l'URL
# que le serveur MCP rend à son client pour aller chercher les images.
PUBLIC_BASE_URL: str = ((_ini("server", "public_base_url",
                              os.getenv("PUBLIC_BASE_URL", "")) or "").rstrip("/")
                        or f"http://127.0.0.1:{AGENT_PORT}")


# ---------- Ollama (traduction des prompts, optionnel) ----------

OLLAMA_ENABLED: bool = _ini_bool("ollama", "enabled", True)
OLLAMA_URL: str = _ini("ollama", "url", os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"))
OLLAMA_TRANSLATE_MODEL: str = _ini("ollama", "translate_model", "translategemma:latest")

# Le modèle est déchargé dès la réponse rendue. Ce n'est pas une optimisation mais
# la condition de la cohabitation : ComfyUI garde 12 à 20 Go de poids résidents
# entre deux jobs, et translategemma (3,3 Go) doit se charger malgré ça.
OLLAMA_KEEP_ALIVE: int = 0


# ---------- Image ----------

DEFAULT_WIDTH: int = _ini_int("image", "default_width", 1024)
DEFAULT_HEIGHT: int = _ini_int("image", "default_height", 1024)

# EmptySD3LatentImage travaille sur un latent 8×, lui-même patché par 2 : toute
# dimension doit être un multiple de 16, sinon ComfyUI arrondit en silence et
# l'image rendue ne fait pas la taille demandée. Le plafond est celui au-delà
# duquel Z-Image Turbo se dégrade franchement.
MIN_SIDE: int = 256
MAX_SIDE: int = 2048
SIDE_STEP: int = 16

# Nombre d'images enchaînées par envoi.
MAX_LOT: int = 4


def snap_side(value) -> int:
    """Ramène une dimension dans les bornes, arrondie au multiple de 16.

    Appliquée **côté serveur** : un client MCP ou un `curl` ne passe pas par le JS
    du navigateur, et c'est ici que se joue la seule garantie que le graphe reçoit
    une dimension valide.

    L'arrondi est un demi vers le haut — 1000 donne 1008, pas 992 — pour coller à
    `Math.round(v / 16) * 16` côté navigateur. Le `round()` de Python, lui, arrondit
    au pair le plus proche : le champ afficherait alors une valeur et le serveur en
    utiliserait une autre.
    """
    try:
        side = int(float(value))
    except (TypeError, ValueError):
        side = DEFAULT_WIDTH
    side = max(MIN_SIDE, min(MAX_SIDE, side))
    side = ((side + SIDE_STEP // 2) // SIDE_STEP) * SIDE_STEP
    return max(MIN_SIDE, min(MAX_SIDE, side))
