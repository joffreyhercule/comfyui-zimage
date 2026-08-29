"""Le seul chemin d'une génération, quel que soit l'appelant.

Web et MCP passent tous les deux par `direct_generate_image` : arrondi des
dimensions, traduction éventuelle, soumission, attente, réclamation du fichier,
**puis insertion en galerie**.

Cette dernière étape se trouve ici, et non dans la route HTTP comme dans le studio
d'origine, précisément parce qu'une image générée par MCP n'apparaissait alors
jamais dans la galerie web : la route était le seul endroit qui insérait, et le
serveur MCP ne la traverse pas.
"""

from __future__ import annotations

import asyncio
import logging

from studio import gallery, translate
from studio.config import COMFY_JOB_TIMEOUT, DEFAULT_HEIGHT, DEFAULT_WIDTH, snap_side
from studio.services import image as image_service

logger = logging.getLogger(__name__)


async def _wait_for_job(job_id: str, timeout: float | None = None,
                        poll: float = 0.5) -> None:
    """Attend la fin d'un job ComfyUI.

    Le plafond vient de `[comfyui] job_timeout`. Abandonner ici **n'annule pas** le
    job côté ComfyUI : il continue de calculer, mais plus personne ne récupérera sa
    sortie — le délai doit donc rester largement au-dessus du pire cas, premier
    chargement de 12 à 20 Go de poids compris.
    """
    from studio.comfy.jobs import JobCancelled, JobStatus, jobs

    if timeout is None:
        timeout = COMFY_JOB_TIMEOUT
    started = asyncio.get_event_loop().time()
    while True:
        job = jobs.get(job_id)
        if not job:
            raise RuntimeError(f"Job {job_id} introuvable")
        status = job.get("status")
        status = status.value if hasattr(status, "value") else str(status)
        if status == JobStatus.COMPLETED.value:
            return
        if status == JobStatus.CANCELLED.value:
            raise JobCancelled(f"Job {job_id} annulé")
        if status == JobStatus.FAILED.value:
            raise RuntimeError(f"Job en échec : {job.get('error') or 'erreur inconnue'}")
        if asyncio.get_event_loop().time() - started > timeout:
            raise TimeoutError(
                f"Job {job_id} abandonné après {timeout:.0f} s "
                "(augmentez [comfyui] job_timeout dans config.ini si c'est trop court)")
        await asyncio.sleep(poll)


async def direct_generate_image(
    prompt: str,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    seed: int | None = None,
    lang: str = "en",
    prompt_source: str = "",
    origin: str = "web",
    persist: bool = True,
    progress_listener=None,
    job_listener=None,
) -> dict:
    """Génère une image et l'inscrit en galerie. Rend le dict décrivant le résultat.

    `prompt_source` est le texte tel que l'utilisateur l'a écrit. Quand il est
    fourni, la traduction a déjà eu lieu en amont (le lot n'en fait qu'une pour
    toutes ses images) et `prompt` est déjà l'anglais à envoyer.

    L'arrondi des dimensions est appliqué **ici**, côté serveur : un client MCP ou
    un `curl` ne passe pas par le JavaScript du navigateur.
    """
    width, height = snap_side(width), snap_side(height)

    source_text = prompt_source or prompt
    if not prompt_source and lang not in ("", "en"):
        translated = await translate.to_english(prompt, lang)
        if translated != prompt:
            logger.info("Prompt traduit (%s -> en)", lang)
        prompt, prompt_source = translated, source_text

    job_id, seed = await image_service.submit_job(
        prompt=prompt, width=width, height=height, seed=seed,
        progress_listener=progress_listener)
    if job_listener:
        job_listener(job_id)

    await _wait_for_job(job_id)
    record = await image_service.finalize_and_persist(job_id=job_id, prompt=prompt)

    result = {
        "url": record["url"],
        "prompt": prompt,
        "prompt_source": prompt_source if prompt_source != prompt else "",
        "lang": lang,
        "width": width,
        "height": height,
        "seed": seed,
    }
    if persist:
        entry = gallery.store.add(
            url=record["url"], prompt=prompt,
            prompt_source=result["prompt_source"], lang=lang,
            width=width, height=height, seed=seed, origin=origin,
            comfy=record.get("comfy"))
        # L'id de galerie fait partie du résultat au lieu d'être agrafé après coup :
        # le front en a besoin pour supprimer ou mettre en favori sans recharger.
        result["entry_id"] = entry["id"]
    return result
