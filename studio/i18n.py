"""Les treize langues du projet, et les chaînes de l'installateur.

Ce module est le **seul** endroit où la liste des langues est écrite. Trois
consommateurs s'en servent, et aucun ne doit pouvoir diverger des autres :

  - `studio/translate.py`, pour la langue source du prompt envoyé à Ollama ;
  - `studio/main.py`, qui expose les noms natifs au sélecteur de l'interface ;
  - `install.py`, qui traduit l'installateur lui-même.

D'où la contrainte qui gouverne tout le fichier : **stdlib pure**. L'installateur
l'importe avant d'avoir installé la moindre dépendance, et `install.bat` l'appelle
même avant le venv, par `python -m studio.i18n <clé>`.

Les chaînes de l'installateur vivent dans `locales/<code>.json`, hors du paquet :
elles ne concernent pas le studio, et un traducteur ne doit pas avoir à ouvrir un
fichier Python pour les corriger. L'anglais sert de base, chaque autre langue ne
fait que le recouvrir — une clé oubliée s'affiche en anglais au lieu de lever.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Les treize langues de l'interface, par leur nom anglais. Le code sert de clé
#: i18n, de langue source présumée du prompt, et d'attribut `lang` du document.
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

#: Le nom de chaque langue DANS cette langue, pour les sélecteurs. Un sélecteur en
#: noms natifs se lit même quand l'interface est dans une langue qu'on ne connaît
#: pas — c'est justement le moment où on en a besoin.
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

#: L'arabe est la seule langue de la liste écrite de droite à gauche.
RTL_LANGUAGES = frozenset({"ar"})

DEFAULT_LANGUAGE = "en"

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"


def normalize(raw: str | None) -> str | None:
    """`fr_FR.UTF-8`, `pt-BR`, `zh_TW` → `fr`, `pt`, `zh`. `None` si inconnue.

    On ne garde que la langue, jamais la région : traduire l'installateur en
    treize langues est déjà beaucoup, le décliner en variantes régionales n'ajoute
    rien qu'un francophone belge ou un lusophone brésilien remarquerait ici.
    """
    if not raw:
        return None
    code = raw.strip().replace("-", "_").split("_")[0].split(".")[0].lower()
    return code if code in LANGUAGES else None


def detect_system_language() -> str:
    """La langue de la machine, ou l'anglais si elle n'est pas des treize.

    Les variables d'environnement passent avant tout : sous Unix elles font foi, et
    sous Windows elles n'existent que si quelqu'un les a posées exprès. Vient
    ensuite l'API Windows — `locale.getlocale()` y rendrait `French_France`, un nom
    de locale Microsoft qu'aucun code ISO ne reconnaît.
    """
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        code = normalize((os.environ.get(variable) or "").split(":")[0])
        if code:
            return code

    if sys.platform == "win32":
        try:
            import ctypes
            import locale as _locale

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            code = normalize(_locale.windows_locale.get(lcid))
            if code:
                return code
        except Exception:  # pas de kernel32, LCID inconnu : on continue
            pass

    try:
        import locale as _locale

        code = normalize(_locale.getdefaultlocale()[0])  # type: ignore[attr-defined]
        if code:
            return code
    except Exception:  # supprimé dans une version future, ou locale illisible
        pass

    return DEFAULT_LANGUAGE


# ---------- Chaînes de l'installateur ----------

_strings: dict[str, str] = {}
_language = DEFAULT_LANGUAGE


def _read(code: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{code}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load(code: str) -> str:
    """Charge les chaînes de `code`, l'anglais en dessous. Rend la langue retenue."""
    global _strings, _language
    _language = code if code in LANGUAGES else DEFAULT_LANGUAGE
    _strings = _read(DEFAULT_LANGUAGE)
    if _language != DEFAULT_LANGUAGE:
        _strings.update(_read(_language))
    return _language


def language() -> str:
    return _language


def t(key: str, **kwargs) -> str:
    """La chaîne de `key`, formatée. Ne lève jamais.

    Une clé absente rend la clé elle-même, un champ manquant rend le gabarit brut :
    un installateur qui plante sur un message est pire qu'un message imparfait.
    """
    if not _strings:
        load(DEFAULT_LANGUAGE)
    template = _strings.get(key, key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


# ---------- Vérification ----------

UI_DIR = Path(__file__).resolve().parent / "static" / "i18n"

#: Les deux jeux de traductions, et qui les lit. Rien d'autre dans le projet ne
#: doit énumérer des langues : c'est ici qu'on vient vérifier que les deux
#: arborescences suivent toujours `LANGUAGES`.
CATALOGS = {"installateur": LOCALES_DIR, "interface": UI_DIR}


def _placeholders(text: str) -> set[str]:
    import re

    return set(re.findall(r"\{(\w+)\}", str(text)))


def check() -> list[str]:
    """Rend la liste des écarts entre les treize langues. Vide = tout est cohérent.

    Une clé oubliée s'affiche en anglais et un champ mal orthographié rend le
    gabarit brut : deux pannes muettes, que seule une comparaison systématique
    avec l'anglais fait apparaître.
    """
    problems: list[str] = []
    for label, directory in CATALOGS.items():
        reference = _read_from(directory, DEFAULT_LANGUAGE)
        if not reference:
            problems.append(f"{label} : {directory / 'en.json'} manquant ou illisible")
            continue
        for code in LANGUAGES:
            path = directory / f"{code}.json"
            if not path.exists():
                problems.append(f"{label}/{code} : fichier absent")
                continue
            try:
                strings = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                problems.append(f"{label}/{code} : JSON invalide ({exc})")
                continue
            for key in reference:
                if key not in strings:
                    problems.append(f"{label}/{code} : clé manquante {key}")
                elif _placeholders(reference[key]) != _placeholders(strings[key]):
                    problems.append(
                        f"{label}/{code} : champs de {key} — attendus "
                        f"{sorted(_placeholders(reference[key]))}, trouvés "
                        f"{sorted(_placeholders(strings[key]))}")
                elif not str(strings[key]).strip():
                    problems.append(f"{label}/{code} : valeur vide pour {key}")
            for key in strings:
                if key not in reference:
                    problems.append(f"{label}/{code} : clé en trop {key}")
        for path in sorted(directory.glob("*.json")):
            if path.stem not in LANGUAGES:
                problems.append(f"{label} : {path.name} ne correspond à aucune langue")
    return problems


def _read_from(directory: Path, code: str) -> dict[str, str]:
    try:
        return json.loads((directory / f"{code}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _main(argv: list[str]) -> int:
    """`python -m studio.i18n [--check | <clé> [champ=valeur ...]]`.

    Sans argument, écrit la langue détectée. Avec une clé, le message traduit :
    `install.bat` et `install.sh` tournent avant le venv et n'ont aucun moyen de
    lire un JSON ni de deviner la langue de l'utilisateur, ils délèguent donc
    leurs trois messages ici. Avec `--check`, compare les treize langues des deux
    catalogues à l'anglais.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if not argv:
        print(detect_system_language())
        return 0
    if argv[0] == "--check":
        problems = check()
        for problem in problems:
            print(problem)
        counts = ", ".join(f"{label} {len(_read_from(directory, DEFAULT_LANGUAGE))} clés"
                           for label, directory in CATALOGS.items())
        print(f"{len(LANGUAGES)} langues — {counts} — "
              + ("tout est cohérent" if not problems else f"{len(problems)} écart(s)"))
        return 1 if problems else 0
    load(detect_system_language())
    fields = dict(item.split("=", 1) for item in argv[1:] if "=" in item)
    print(t(argv[0], **fields))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
