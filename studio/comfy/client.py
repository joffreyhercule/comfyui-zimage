"""Client ComfyUI asynchrone : construire le graphe, soumettre, suivre, récupérer.

Quatre invariants tiennent tout le suivi de progression et l'annulation ; ils sont
signalés à l'endroit où ils s'appliquent, parce que chacun casse en silence :
ouvrir le WebSocket avant de poster, plafonner les trames à 64 Mio, remonter le
`prompt_id` pendant le job, et ne jamais interrompre sans ce `prompt_id`.
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import date
from pathlib import Path

import httpx
import websockets

from studio.config import COMFY_OUTPUT_DIR, MODEL_CLIP, MODEL_UNET, MODEL_VAE
from studio.comfy.config import COMFYUI_URL, load_workflow, load_workflow_mapping

logger = logging.getLogger(__name__)

COMFYUI_WS_URL = COMFYUI_URL.replace("http://", "ws://").replace("https://", "wss://")

# ComfyUI pousse ses aperçus en trames BINAIRES sur le même socket que les
# événements JSON. `websockets` plafonne à 1 Mio par défaut : un aperçu dépasse,
# la connexion meurt en 1009, et le suivi de progression est perdu pour tout le
# job — que ComfyUI, lui, continue de calculer. On jette ces trames de toute façon
# (voir `_consume_ws_until_done`) ; le plafond reste fini pour qu'une trame folle
# ne mange pas la mémoire.
WS_MAX_FRAME_BYTES = 64 * 1024 * 1024


class JobCancelled(Exception):
    """Le prompt suivi a été interrompu avant d'avoir produit une sortie.

    Levée sur `execution_interrupted`, que ComfyUI émet aussi bien quand le studio
    annule que quand quelqu'un appuie sur stop dans l'interface de ComfyUI."""


# ---------- Construction du graphe ----------

def _set_input(workflow: dict, inputs_map: dict, key: str, value) -> None:
    """Pose une valeur dans le graphe d'après le mapping. Une clé absente du
    mapping est ignorée : c'est ce qui permet d'ajouter une entrée sans toucher
    aux workflows qui ne la déclarent pas."""
    spec = inputs_map.get(key)
    if spec and spec["node"] in workflow:
        workflow[spec["node"]]["inputs"][spec["field"]] = value


def build_workflow(prompt: str, width: int, height: int, seed: int,
                   workflow_name: str = "zimage") -> dict:
    """Charge le graphe et y injecte prompt, dimensions, seed et noms de modèles.

    `seed` est **obligatoire** et n'est pas tiré ici : une valeur tirée à l'intérieur
    ne remonterait jamais à l'appelant, donc ne serait ni affichable ni réutilisable.
    C'est `submit_job` qui tire et qui rend la valeur.

    Les noms de modèles viennent de `config.ini`, pas du JSON : l'installateur
    choisit la variante (int8 sur CUDA, bf16 ailleurs) et le graphe reste le même.
    """
    workflow = load_workflow(workflow_name)
    mapping = load_workflow_mapping(workflow_name)
    inputs_map = mapping.get("inputs", {})

    _set_input(workflow, inputs_map, "prompt", prompt)
    _set_input(workflow, inputs_map, "seed", seed)
    _set_input(workflow, inputs_map, "width", width)
    _set_input(workflow, inputs_map, "height", height)
    _set_input(workflow, inputs_map, "unet", MODEL_UNET)
    _set_input(workflow, inputs_map, "clip", MODEL_CLIP)
    _set_input(workflow, inputs_map, "vae", MODEL_VAE)

    # Les sorties sont rangées par jour côté ComfyUI aussi : quand le déplacement
    # vers media/ échoue, ce qui reste dans son output reste au moins lisible.
    wf_type = mapping.get("type", "image")
    _set_input(workflow, inputs_map, "output_prefix",
               f"{date.today().isoformat()}/{wf_type}")
    return workflow


# ---------- Soumission et suivi ----------

async def _post_prompt(workflow: dict, client_id: str) -> str:
    """POST /prompt, rend le `prompt_id` attribué par ComfyUI."""
    payload = {"prompt": workflow, "client_id": client_id}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{COMFYUI_URL}/prompt", json=payload)
        data = response.json()
        if response.status_code != 200:
            message = data.get("error", {}).get("message", response.text)
            node_errors = data.get("node_errors", {})
            logger.error("ComfyUI a refusé le prompt : %s | node_errors: %s",
                         message, json.dumps(node_errors, default=str)[:2000])
            raise RuntimeError(f"ComfyUI: {message}")
        return data["prompt_id"]


_SAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}


def _find_sampler_nodes(workflow: dict) -> set[str]:
    """Les nodes dont la progression compte. Le reste du graphe (chargement des
    modèles, VAE decode) n'émet rien d'exploitable pour une barre."""
    return {node_id for node_id, node in workflow.items()
            if node.get("class_type") in _SAMPLER_TYPES}


async def _consume_ws_until_done(ws, prompt_id: str, sampler_nodes: set[str],
                                 progress_callback=None) -> None:
    """Draine les événements du prompt jusqu'au marqueur final `executing`/null."""
    total_samplers = max(len(sampler_nodes), 1)
    sampler_order: list[str] = []
    current_node: str | None = None

    while True:
        raw = await ws.recv()
        if isinstance(raw, bytes):
            continue  # aperçu binaire : reçu pour être jeté (voir WS_MAX_FRAME_BYTES)
        message = json.loads(raw)
        kind = message.get("type")
        data = message.get("data", {})

        if data.get("prompt_id") != prompt_id:
            continue

        if kind == "executing":
            node = data.get("node")
            if node is None:
                return
            current_node = node
            if node in sampler_nodes and node not in sampler_order:
                sampler_order.append(node)
                # Émettre le vrai 0 % AVANT le premier `progress` : à 15 steps, le
                # premier événement arrive déjà à 1/15, et le départ serait sauté.
                if progress_callback:
                    await progress_callback(sampler_order.index(node) / total_samplers)

        elif kind == "progress":
            value, maximum = data.get("value", 0), data.get("max", 1)
            node_progress = value / maximum if maximum else 0
            if current_node in sampler_nodes and progress_callback:
                index = (sampler_order.index(current_node)
                         if current_node in sampler_order else 0)
                await progress_callback((index + node_progress) / total_samplers)

        elif kind == "execution_interrupted":
            # ComfyUI enverra quand même `executing`/null juste après, mais il n'y
            # aura jamais de sortie : inutile d'aller interroger un history vide.
            raise JobCancelled(f"Prompt {prompt_id} interrompu")

        elif kind == "execution_error":
            raise RuntimeError(
                f"Erreur d'exécution ComfyUI : {data.get('exception_message', 'inconnue')}")


async def submit_and_wait(workflow: dict, progress_callback=None, on_queued=None) -> str:
    """Ouvre le WebSocket, PUIS soumet, PUIS draine les événements.

    L'ordre n'est pas cosmétique : modèle déjà chargé, ComfyUI commence à exécuter
    avant la fin du handshake, le premier `executing` passe à côté, `current_node`
    reste None et la barre affiche 0 % pendant tout le job.

    `on_queued(prompt_id)` est appelé dès que ComfyUI accepte le prompt, donc bien
    avant le retour de cette coroutine : l'annulation a besoin de l'identifiant
    pendant que le job tourne, elle ne peut pas attendre la valeur de retour.
    """
    sampler_nodes = _find_sampler_nodes(workflow)
    client_id = uuid.uuid4().hex
    ws_url = f"{COMFYUI_WS_URL}/ws?clientId={client_id}"
    async with websockets.connect(ws_url, max_size=WS_MAX_FRAME_BYTES) as ws:
        prompt_id = await _post_prompt(workflow, client_id)
        if on_queued is not None:
            on_queued(prompt_id)
        await _consume_ws_until_done(ws, prompt_id, sampler_nodes, progress_callback)
    return prompt_id


async def cancel_prompt(prompt_id: str) -> None:
    """Arrête un prompt, qu'il soit en cours ou encore dans la file.

    Les deux appels sont émis parce qu'on ignore dans quel état il se trouve, et
    chacun est sans effet dans l'autre état. `prompt_id` est obligatoire sur
    `/interrupt` : sans corps, c'est un arrêt GLOBAL de ComfyUI, qui tuerait aussi
    ce que l'utilisateur y fait par ailleurs.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(f"{COMFYUI_URL}/queue", json={"delete": [prompt_id]})
            await client.post(f"{COMFYUI_URL}/interrupt", json={"prompt_id": prompt_id})
        except Exception as exc:
            logger.warning("Annulation du prompt %s en échec : %s", prompt_id, exc)


# ---------- Récupération du résultat ----------

async def get_result(prompt_id: str) -> dict | None:
    """L'history du prompt, réduit au premier fichier de sortie déclaré."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
        response.raise_for_status()
        history = response.json()

    if prompt_id not in history:
        return None
    for node_output in history[prompt_id].get("outputs", {}).values():
        images = node_output.get("images", [])
        if images:
            return images[0]
    return None


async def get_file_bytes(filename: str, subfolder: str = "",
                         file_type: str = "output") -> bytes:
    """Télécharge un fichier de sortie par l'API `/view` de ComfyUI."""
    params = {"filename": filename, "subfolder": subfolder, "type": file_type}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.get(f"{COMFYUI_URL}/view", params=params)
        response.raise_for_status()
        return response.content


def local_output_path(filename: str, subfolder: str = "",
                      file_type: str = "output") -> Path | None:
    """Chemin disque du fichier de sortie, quand ComfyUI écrit sur cette machine.

    Rend None dès que le fichier n'est pas atteignable directement — ComfyUI
    distant, `output_dir` mal renseigné, sortie temporaire, ou chemin qui s'échappe
    du dossier de sortie. L'appelant retombe alors sur `/view`.
    """
    if not filename or file_type != "output":
        return None
    base = COMFY_OUTPUT_DIR
    if not base or not base.exists():
        return None
    try:
        target = (base / (subfolder or "") / filename).resolve()
        target.relative_to(base.resolve())  # lève ValueError si hors du dossier
    except (ValueError, OSError):
        logger.warning("Sortie ComfyUI hors de %s : %s/%s", base, subfolder, filename)
        return None
    return target if target.is_file() else None


def _move_output(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dest)
    except OSError:
        # Volumes différents : copie puis suppression. Plus atomique, mais toujours
        # mieux que de laisser l'original derrière soi.
        shutil.move(str(src), str(dest))


async def fetch_output_to(info: dict, dest: Path) -> bool:
    """Amène le fichier produit jusqu'à `dest`. Rend True s'il a été DÉPLACÉ.

    ComfyUI écrit dans son propre dossier de sortie, sur le même volume que
    `media/` : le déplacement est donc instantané, atomique, et l'octet n'existe
    jamais en double. Le repli HTTP `/view` laisse l'original en place — d'où le
    booléen : l'appelant ne note l'origine `comfy_*` que dans ce cas, sinon la
    galerie tenterait plus tard de supprimer un fichier qui n'existe plus.
    """
    filename = info.get("filename")
    subfolder = info.get("subfolder", "")
    file_type = info.get("type", "output")

    src = local_output_path(filename, subfolder, file_type)
    if src is not None:
        try:
            await asyncio.to_thread(_move_output, src, dest)
            logger.info("Sortie ComfyUI déplacée : %s -> %s", src, dest)
            return True
        except OSError as exc:
            # La source est toujours là : le téléchargement reste une porte de sortie.
            logger.warning("Déplacement de %s échoué (%s), repli sur /view", src, exc)

    data = await get_file_bytes(filename, subfolder, file_type)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return False


async def check_health() -> bool:
    """ComfyUI répond-il ?"""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{COMFYUI_URL}/system_stats")
            return response.status_code == 200
    except Exception:
        return False
