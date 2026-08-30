"""Traduction du prompt vers l'anglais, par Ollama — optionnelle par construction.

À ne pas confondre avec la langue de l'interface : celle-là vient de fichiers
`static/i18n/<code>.json` statiques et ne dépend d'aucun service. Ici, il s'agit
uniquement du texte envoyé au modèle d'image, qui comprend nettement mieux
l'anglais. Sans Ollama, le prompt part verbatim et rien ne casse.
"""

import logging

from studio.config import (
    OLLAMA_ENABLED,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_TRANSLATE_MODEL,
    OLLAMA_URL,
)
# Les treize langues vivent dans `studio/i18n.py`, seul module que l'installateur
# puisse importer avant d'avoir installé quoi que ce soit. On les réexporte ici :
# `main.py` et le reste du studio continuent de les lire depuis `translate`.
from studio.i18n import LANGUAGES, NATIVE_NAMES, RTL_LANGUAGES  # noqa: F401

logger = logging.getLogger(__name__)


def _system_prompt(lang: str) -> str:
    """Prompt système de translategemma, paramétré par la langue source.

    Le modèle du studio d'origine avait « French » codé en dur, ce qui aurait
    traduit de travers pour les douze autres langues.
    """
    name = LANGUAGES.get(lang, lang)
    return (
        f"You are a professional {name} ({lang}) to English (en) translator. "
        f"Your goal is to accurately convey the meaning and nuances of the original "
        f"{name} text while adhering to English grammar, vocabulary, and cultural "
        "sensitivities.\n"
        "Produce only the English translation, without any additional explanations "
        f"or commentary. Please translate the following {name} text into English:"
    )


def is_available() -> bool:
    """Vrai si la traduction est censée fonctionner. C'est une intention, pas une
    garantie : le service peut être arrêté, et `to_english` le gère seul."""
    return bool(OLLAMA_ENABLED)


async def to_english(text: str, lang: str) -> str:
    """Traduit `text` depuis `lang` vers l'anglais. Rend le texte d'origine en cas
    d'échec, quel qu'il soit.

    Court-circuité sans le moindre appel réseau quand la langue est déjà l'anglais,
    quand le texte est vide, ou quand Ollama est désactivé dans `config.ini` : dans
    ces cas, charger un modèle de 3,3 Go pour ne rien faire serait absurde.

    Un prompt déjà écrit en anglais alors que l'interface est en français traverse
    translategemma sans dommage, et l'événement `translated` montre de toute façon
    au client ce qui est réellement parti au modèle.
    """
    text = (text or "").strip()
    if not text or lang in ("", "en") or not OLLAMA_ENABLED:
        return text

    try:
        import ollama

        client = ollama.AsyncClient(host=OLLAMA_URL)
        response = await client.chat(
            model=OLLAMA_TRANSLATE_MODEL,
            messages=[
                {"role": "system", "content": _system_prompt(lang)},
                {"role": "user", "content": text},
            ],
            # Déchargement immédiat : ComfyUI garde 12 à 20 Go de poids résidents
            # entre deux jobs, et translategemma doit tenir à côté.
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
        translated = (response.message.content or "").strip()
        return translated or text
    except Exception as exc:  # noqa: BLE001 — Ollama arrêté doit dégrader, pas casser
        logger.warning("Traduction indisponible (%s) : le prompt part verbatim", exc)
        return text
