"""Exécution d'un lot de générations, et les événements qu'il émet.

Remplace la boucle d'agent du studio d'origine : il n'y a plus de LLM créatif ici,
donc plus de décision à prendre. Un envoi = un prompt, une langue, des dimensions,
et de une à quatre images produites l'une après l'autre.

Contrat WebSocket (chaque événement porte `conversation_id`, et `item_index` dès
qu'il concerne une image précise) :

    user_message  le prompt tel que l'utilisateur l'a écrit
    translated    ce qui est réellement parti au modèle (absent si rien n'a changé)
    progress      {value: 0..1, status, item_index}
    tool_result   {media: [...], item_index} — une image finie
    cancelled     {item_index}
    error         {message}
    done          fin du lot, quoi qu'il soit arrivé
"""

from __future__ import annotations

import logging

from studio import dispatcher, generation, translate
from studio.comfy.client import JobCancelled
from studio.config import MAX_LOT

logger = logging.getLogger(__name__)


async def run_lot(prompt: str, lang: str, width: int, height: int, lot: int,
                  seed: int | None, conversation_id: str, on_event) -> None:
    """Génère `lot` images à partir du même prompt et pousse les événements.

    N'élève jamais : toute erreur devient un événement, sinon le client resterait
    avec un carré de chargement qui ne se remplit plus.
    """
    lot = max(1, min(MAX_LOT, int(lot or 1)))
    prompt = (prompt or "").strip()

    await on_event({"type": "user_message", "content": prompt})
    if not prompt:
        await on_event({"type": "error", "message": "Prompt vide"})
        await on_event({"type": "done"})
        return

    # UNE traduction pour tout le lot. Traduire par image rechargerait
    # translategemma quatre fois, alors que le texte est le même à chaque fois.
    english = await translate.to_english(prompt, lang)
    prompt_source = prompt if english != prompt else ""
    if prompt_source:
        await on_event({"type": "translated", "source": prompt,
                        "translated": english, "lang": lang})

    try:
        for index in range(lot):
            slot = generation.open_slot(conversation_id, index)
            if generation.is_cancelled(slot):
                # Annulé avant même son tour : les images d'un lot sont produites
                # l'une après l'autre, et c'est justement en attendant que
                # l'utilisateur renonce.
                generation.close_slot(slot)
                await on_event({"type": "cancelled", "item_index": index})
                continue

            async def progress_listener(value: float, _index: int = index) -> None:
                await on_event({"type": "progress", "value": value,
                                "status": "running", "item_index": _index})

            def job_listener(job_id: str, _slot: str = slot) -> None:
                generation.bind_job(_slot, job_id)

            # Seed épinglé : `seed + i`, sinon les quatre images seraient
            # rigoureusement identiques — le seul résultat qu'on ne peut pas vouloir.
            item_seed = None if seed is None else int(seed) + index

            try:
                # `prompt_source` porte TOUJOURS le texte d'origine, même quand la
                # traduction ne l'a pas changé : c'est ce qui dit au dispatcher que
                # la traduction a déjà eu lieu. Sans cela il la referait pour chaque
                # image du lot, soit un chargement de translategemma par image.
                # Le dispatcher remet le champ à vide s'il est identique au prompt.
                result = await dispatcher.direct_generate_image(
                    prompt=english, width=width, height=height, seed=item_seed,
                    lang=lang, prompt_source=prompt, origin="web",
                    progress_listener=progress_listener, job_listener=job_listener)
            except JobCancelled:
                await on_event({"type": "cancelled", "item_index": index})
                continue
            except Exception as exc:  # noqa: BLE001 — remonté au client, pas avalé
                logger.exception("Génération %d en échec", index)
                await on_event({"type": "tool_result", "text": f"Error: {exc}",
                                "media": [], "item_index": index})
                continue
            finally:
                generation.close_slot(slot)

            # 1.0 synthétique : ComfyUI cesse souvent d'émettre avant 100 %, et
            # sans cela le carré ne finit pas son remplissage avant de basculer
            # sur l'image.
            await on_event({"type": "progress", "value": 1.0,
                            "status": "completed", "item_index": index})
            await on_event({"type": "tool_result", "media": [result],
                            "item_index": index})
    finally:
        generation.clear_conversation(conversation_id)
        await on_event({"type": "done"})
