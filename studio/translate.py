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

logger = logging.getLogger(__name__)

#: Les treize langues de l'interface. Le code sert de clé i18n, de langue source
#: présumée du prompt, et d'attribut `lang` du document HTML.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
}

#: Le nom de chaque langue DANS cette langue, pour le sélecteur et la galerie.
#: Distinct de `LANGUAGES`, qui porte les noms anglais parce que c'est ce que le
#: prompt système de traduction attend. Un sélecteur en noms natifs se lit même
#: quand l'interface est dans une langue qu'on ne connaît pas — c'est justement le
#: moment où on en a besoin.
NATIVE_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "nl": "Nederlands",
    "pl": "Polski",
    "ru": "Русский",
    "ja": "日本語",
    "zh": "中文",
    "ko": "한국어",
    "ar": "العربية",
}

#: L'arabe est la seule langue de la liste écrite de droite à gauche. Le front s'en
#: sert pour basculer `document.documentElement.dir`.
RTL_LANGUAGES = frozenset({"ar"})


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
