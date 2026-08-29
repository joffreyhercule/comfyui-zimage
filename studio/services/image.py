"""Génération d'image : soumission du job, puis réclamation du fichier produit.

`finalize_and_persist` rend un enregistrement ; il n'insère rien en base. C'est le
dispatcher qui écrit en galerie, parce qu'il est le seul point que traversent à la
fois le chemin web et le chemin MCP.
"""

import asyncio
import logging
import random
from datetime import datetime
from uuid import uuid4

from studio.comfy import client as comfy_client
from studio.comfy.jobs import JobStatus, jobs, run_job
from studio.config import MEDIA_DIR

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_WORKFLOW = "zimage"

# Borne du tirage de seed. 2**53 est le dernier entier que JavaScript représente
# exactement : au-delà, le seed affiché dans la galerie ne serait plus celui qui a
# produit l'image, et « réutiliser ce seed » rendrait une autre image.
SEED_MAX = 2 ** 53


async def submit_job(prompt: str, width: int = 1024, height: int = 1024,
                     seed: int | None = None, progress_listener=None) -> tuple[str, int]:
    """Soumet une génération et rend `(job_id, seed)`.

    Le seed est tiré **ici** et rendu, au lieu d'être tiré au fond de
    `build_workflow` : sans cela, la valeur qui a produit l'image n'est connue de
    personne, donc ni affichable, ni enregistrable, ni réutilisable.
    """
    if seed is None:
        seed = random.randint(0, SEED_MAX)
    seed = int(seed)

    workflow = comfy_client.build_workflow(prompt=prompt, width=width, height=height,
                                           seed=seed,
                                           workflow_name=DEFAULT_IMAGE_WORKFLOW)
    job_id = uuid4().hex
    jobs[job_id] = {
        "status": JobStatus.QUEUED,
        "progress": 0.0,
        "result": None,
        "error": None,
        "prompt_id": None,
        "seed": seed,
    }
    logger.info("Job image lancé : %s (seed=%s, %dx%d)", job_id, seed, width, height)

    # La tâche ouvre le WebSocket AVANT de poster le prompt ; son handle est gardé
    # sur le job pour que `cancel_job` puisse la tuer quand le prompt est retiré de
    # la file plutôt qu'interrompu (aucun événement ne reviendrait jamais).
    jobs[job_id]["task"] = asyncio.create_task(
        run_job(job_id, workflow, progress_listener=progress_listener))
    return job_id, seed


async def get_job_status(job_id: str) -> dict:
    if job_id not in jobs:
        raise RuntimeError(f"Job {job_id} inconnu")
    job = jobs[job_id]
    return {"job_id": job_id, "status": job["status"],
            "progress": job["progress"], "error": job["error"]}


def _require_completed(job_id: str) -> dict:
    """Le descripteur de sortie d'un job qui s'est terminé avec succès."""
    if job_id not in jobs:
        raise RuntimeError(f"Job {job_id} inconnu")
    job = jobs[job_id]
    if job["status"] == JobStatus.FAILED:
        raise RuntimeError(f"Job en échec : {job['error']}")
    if job["status"] != JobStatus.COMPLETED:
        raise RuntimeError("Job pas encore terminé")
    return job["result"]


def get_job_result_info(job_id: str) -> dict | None:
    """`{filename, subfolder, type}` du résultat, ou None."""
    job = jobs.get(job_id)
    info = job.get("result") if job else None
    if not isinstance(info, dict):
        return None
    return {"filename": info.get("filename"),
            "subfolder": info.get("subfolder", ""),
            "type": info.get("type", "output")}


async def finalize_and_persist(job_id: str, prompt: str = "") -> dict:
    """Réclame la sortie dans `media/<AAAA-MM-JJ>/img_<id>.png` et rend un record.

    `fetch_output_to` DÉPLACE le fichier quand ComfyUI écrit sur cette machine :
    l'image n'existe alors qu'une fois, l'origine ComfyUI n'existe plus, et la
    conserver ferait tenter plus tard une suppression fantôme. Elle n'est donc
    notée que dans le cas contraire — ComfyUI distant, fichier récupéré en HTTP et
    resté dans son dossier de sortie, que la galerie devra bien supprimer un jour.
    """
    _require_completed(job_id)
    comfy_origin = get_job_result_info(job_id)

    image_id = uuid4().hex[:12]
    today = datetime.now().strftime("%Y-%m-%d")
    image_path = MEDIA_DIR / today / f"img_{image_id}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)

    moved = await comfy_client.fetch_output_to(comfy_origin or {}, image_path)
    # Un PNG de moins de 100 octets n'est pas une image : mieux vaut échouer ici
    # que d'inscrire en galerie une vignette cassée.
    if not image_path.exists() or image_path.stat().st_size < 100:
        image_path.unlink(missing_ok=True)
        raise RuntimeError("Image invalide reçue de ComfyUI")

    record = {
        "id": image_id,
        "url": f"/media/{today}/img_{image_id}.png",
        "prompt": prompt,
    }
    if not moved and comfy_origin and comfy_origin.get("filename"):
        record["comfy"] = comfy_origin
    return record
