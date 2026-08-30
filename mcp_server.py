"""Serveur MCP de comfyui-zimage.

Expose la génération d'image aux clients MCP (Claude Code, Claude Desktop…) en
appelant le dispatcher du studio **en direct** : aucun serveur HTTP n'a besoin de
tourner, seul ComfyUI doit répondre sur son port.

Conséquence importante : ce serveur est un autre processus que le studio. Les deux
ne partagent que `db/gallery.json`, et c'est le dispatcher — traversé par les deux
chemins — qui écrit en galerie. Une image générée ici apparaît donc bien dans la
galerie web, au prochain rafraîchissement de celle-ci.
"""

import logging
import sys
from pathlib import Path

# SDK mcp 2.x : FastMCP s'appelle MCPServer. Migrer plutôt qu'épingler `mcp<2`,
# pour ne pas figer un projet neuf sur une API dépréciée.
from mcp.server.mcpserver import Image, MCPServer

# Rend `studio.*` importable quand ce script est lancé en sous-processus par un
# client MCP, depuis n'importe quel répertoire de travail.
_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

from studio import dispatcher  # noqa: E402
from studio.config import MAX_SIDE, MEDIA_DIR, MIN_SIDE, PUBLIC_BASE_URL  # noqa: E402
from studio.i18n import LANGUAGES  # noqa: E402

logger = logging.getLogger(__name__)

mcp = MCPServer(
    name="comfyui-zimage",
    instructions=(
        "This server generates images locally with Z-Image Turbo through ComfyUI.\n"
        "  * generate_image: text-to-image. `width` and `height` are independent and "
        "rounded server-side to a multiple of 16, clamped to "
        f"{MIN_SIDE}-{MAX_SIDE}. `seed` makes a result reproducible. `lang` is the "
        "language the prompt is written in; anything other than 'en' is translated "
        "to English first when Ollama is available.\n"
        "  * get_media: fetch a previously generated image (preview + Download URL).\n"
        "Each result includes a 'Download URL:' line — an absolute HTTP URL to "
        "download the raw bytes, valid while the studio is running — and a "
        "'Server path:' line, the path to pass back to get_media."
    ),
)

_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}


def _normalize_source(path: str | None) -> str | None:
    """Retire le préfixe public d'une URL pour retrouver un chemin `/media/...`.

    Un client réutilise volontiers l'URL absolue qu'on lui a donnée ; seul le chemin
    serveur est résoluble ici.
    """
    if not path:
        return path
    if PUBLIC_BASE_URL and path.startswith(PUBLIC_BASE_URL):
        return path[len(PUBLIC_BASE_URL):]
    return path


def _public_url(server_path: str) -> str:
    """Transforme un chemin `/media/...` en URL absolue téléchargeable."""
    if PUBLIC_BASE_URL and server_path.startswith("/media/"):
        return f"{PUBLIC_BASE_URL}{server_path}"
    return server_path


def _resolve_local(server_path: str) -> Path:
    """Chemin disque d'un `/media/...` (ou d'un chemin absolu déjà donné tel quel)."""
    path = _normalize_source(server_path) or ""
    if path.startswith("/media/"):
        return MEDIA_DIR / path[len("/media/"):]
    return Path(path)


def _origin_marker(result: dict) -> str:
    """Origine ComfyUI, au format texte analysable. Vide quand le fichier a été
    déplacé — il n'y a alors plus d'original à désigner."""
    comfy = result.get("comfy") or {}
    filename = comfy.get("filename")
    if not filename:
        return ""
    return (f"[comfy_origin] subfolder={comfy.get('subfolder', '')} "
            f"filename={filename} type={comfy.get('type', 'output')}")


def _with_language_codes(func):
    """Pose la liste des langues dans la docstring, depuis `studio/i18n.py`.

    L'énumérer à la main ici en ferait une quatorzième copie, que rien ne
    rappellerait de mettre à jour. Le remplacement passe par `replace` et non par
    `format` : une accolade ajoutée un jour dans la docstring ferait lever le
    second, à l'import du serveur.

    Placé sous `@mcp.tool()`, donc appliqué avant lui : le SDK lit la docstring
    déjà complétée.
    """
    func.__doc__ = func.__doc__.replace(
        "{codes}", ", ".join(f"'{code}'" for code in LANGUAGES))
    return func


@mcp.tool()
@_with_language_codes
async def generate_image(prompt: str, width: int = 1024, height: int = 1024,
                         seed: int | None = None, lang: str = "en") -> list:
    """Generate an image from a text prompt with Z-Image Turbo.

    Args:
        prompt: Text description of the image to generate.
        width: Width in pixels. Rounded server-side to a multiple of 16 and clamped
            to 256-2048 (the graph's empty latent requires it). Default 1024.
        height: Height in pixels, same rounding and bounds. Independent of `width`,
            so any aspect ratio is available. Default 1024.
        seed: Optional seed, for a reproducible result. Omitted, one is drawn and
            reported back so the image can be reproduced later.
        lang: ISO code of the language the prompt is written in ({codes}). Anything
            other than 'en' is translated to English before generation when Ollama
            is available; otherwise the prompt is sent verbatim.

    Returns:
        Server path, Download URL, the actual settings used, and a visual preview.
    """
    result = await dispatcher.direct_generate_image(
        prompt=prompt, width=width, height=height, seed=seed, lang=lang,
        origin="mcp")

    image_path = result["url"]
    out = [
        f"Server path: {image_path}",
        f"Download URL: {_public_url(image_path)}",
        f"Settings: {result['width']}x{result['height']}, seed={result['seed']}",
    ]
    if result.get("prompt_source"):
        out.append(f"Prompt sent to the model: {result['prompt']}")
    marker = _origin_marker(result)
    if marker:
        out.append(marker)

    local = _resolve_local(image_path)
    if local.exists():
        out.append(Image(data=local.read_bytes(), format="png"))
    return out


@mcp.tool()
async def get_media(media_path: str) -> list:
    """Fetch a previously generated image.

    Args:
        media_path: Server path or Download URL returned by generate_image.

    Returns:
        The Download URL and a visual preview of the image.
    """
    local = _resolve_local(media_path)
    if not local.exists():
        raise FileNotFoundError(f"Media not found: {media_path}")
    extension = local.suffix.lstrip(".").lower()
    if extension not in _IMAGE_EXTS:
        raise ValueError(f"Not an image: {media_path}")
    return [
        f"Download URL: {_public_url(_normalize_source(media_path))}",
        Image(data=local.read_bytes(), format=extension or "png"),
    ]


if __name__ == "__main__":
    mcp.run("stdio")
