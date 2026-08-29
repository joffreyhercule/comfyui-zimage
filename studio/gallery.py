"""Galerie : une base TinyDB où **un document = une image**.

Le studio et le serveur MCP sont deux processus distincts qui ne partagent que ce
fichier. Deux mécanismes le rendent viable, et ils ne sont pas décoratifs :

- un cache invalidé au `mtime_ns` — sans lui, chaque processus resterait aveugle
  aux écritures de l'autre pour toute la durée de sa vie ;
- un verrou exclusif autour des écritures, sur un `db/gallery.lock` dédié — jamais
  sur la base elle-même, que TinyDB réécrit entièrement à chaque insertion.

Limite acceptée : une image générée par MCP n'apparaît dans un onglet déjà ouvert
qu'au prochain rafraîchissement de la galerie. Un canal de notification entre
processus ne vaudrait pas son prix ici.
"""

import logging
import os
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from tinydb import Query, TinyDB
from tinydb.middlewares import CachingMiddleware
from tinydb.storages import JSONStorage

from studio.config import (
    COMFY_OUTPUT_DIR,
    GALLERY_DB_PATH,
    GALLERY_LOCK_PATH,
    MEDIA_DIR,
)

logger = logging.getLogger(__name__)


def _generate_id() -> str:
    return uuid4().hex[:12]


def _normalize(text: str) -> str:
    """Minuscules, accents retirés : une recherche « chateau » trouve « château »."""
    text = (text or "").lower()
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


# ---------- Verrou inter-processus ----------

class _CrossProcessLock:
    """Verrou exclusif sur un fichier dédié, tenu le temps d'une écriture.

    Les deux implémentations n'ont en commun que leur effet : `msvcrt.locking`
    verrouille un octet et lève après une dizaine de secondes d'attente,
    `fcntl.flock` bloque jusqu'à obtention. C'est le seul endroit du projet où
    Windows et Unix divergent au niveau du code, d'où l'isolement ici.
    """

    def __init__(self, path: Path):
        self._path = path
        self._handle = None
        self._thread_lock = threading.Lock()  # les tâches du même processus aussi

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "a+b")
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            # Un verrou impossible à poser ne doit pas empêcher d'écrire : au pire
            # deux processus se marchent dessus, ce qui reste préférable à une
            # galerie qui refuse d'enregistrer.
            logger.warning("[gallery] verrou indisponible (%s), écriture sans verrou", exc)
            self._close_handle()
        return self

    def __exit__(self, *exc):
        try:
            if self._handle is not None:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._close_handle()
            self._thread_lock.release()

    def _close_handle(self):
        try:
            if self._handle is not None:
                self._handle.close()
        except OSError:
            pass
        self._handle = None


# ---------- Suppression de fichiers ----------

def _safe_unlink(path: Path, base: Path) -> bool:
    """Supprime `path` seulement s'il se résout **dans** `base`.

    C'est la seule protection contre une traversée de répertoire depuis une URL de
    galerie : le document est une donnée persistée, pas une entrée de confiance.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(base.resolve())  # lève ValueError si en dehors
    except (ValueError, OSError) as exc:
        logger.warning("[gallery] suppression refusée pour %s (hors de %s) : %s",
                       path, base, exc)
        return False
    try:
        if resolved.exists():
            resolved.unlink()
            logger.info("[gallery] fichier supprimé : %s", resolved)
            return True
    except OSError as exc:
        logger.warning("[gallery] suppression impossible de %s : %s", resolved, exc)
    return False


def _delete_media_file(url: str) -> None:
    """Résout une URL `/media/...` et supprime le fichier correspondant."""
    if not url:
        return
    path = unquote(urlparse(url).path or url)
    if not path.startswith("/media/"):
        return
    _safe_unlink(MEDIA_DIR / path[len("/media/"):], MEDIA_DIR)


def _delete_comfy_source(filename: str | None, subfolder: str = "",
                         file_type: str = "output") -> None:
    """Supprime le fichier d'origine resté dans la sortie de ComfyUI.

    N'existe que pour les images récupérées par `/view` : quand le fichier a été
    déplacé, aucun champ `comfy_*` n'est enregistré et rien n'est tenté ici.
    """
    if not filename:
        return
    base = COMFY_OUTPUT_DIR
    if not base or not base.exists():
        return
    _safe_unlink(base / (subfolder or "") / filename, base)


# ---------- Base ----------

class _FlushingCache(CachingMiddleware):
    """Cache TinyDB qui écrit sur disque à CHAQUE modification.

    Le gain visé est la lecture : sans cache, JSONStorage reparse tout le fichier à
    chaque accès. Le défaut de TinyDB (1000 écritures bufferisées) perdrait en
    revanche tout un lot au moindre Ctrl+C — et, ici, laisserait surtout l'autre
    processus lire une base périmée.
    """

    WRITE_CACHE_SIZE = 1


def _open_db(path) -> TinyDB:
    """Ouvre une TinyDB avec son PROPRE cache.

    Une instance de middleware ne se partage pas : `Middleware.__call__` écrase
    `self.storage`, donc deux TinyDB construites sur le même objet finissent par
    partager un cache et se corrompre.

    `encoding="utf-8"` est transmis à `open()` : sans lui, TinyDB retombe sous
    Windows sur cp1252 et massacre les accents (é devient Ã©).
    """
    return TinyDB(str(path), storage=_FlushingCache(JSONStorage),
                  encoding="utf-8", ensure_ascii=False)


class GalleryStore:
    """Accès à la galerie, sûr entre threads et entre processus."""

    def __init__(self):
        GALLERY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _CrossProcessLock(GALLERY_LOCK_PATH)
        self._db = None
        self._table = None
        self._mtime = -1
        self._open()

    # -- cycle de vie --

    def _open(self) -> None:
        self._db = _open_db(GALLERY_DB_PATH)
        self._table = self._db.table("gallery")
        self._mtime = self._disk_mtime()

    def _disk_mtime(self) -> int:
        try:
            return GALLERY_DB_PATH.stat().st_mtime_ns
        except OSError:
            return -1

    def _sync(self) -> None:
        """Rouvre la base si un autre processus l'a réécrite depuis notre dernier
        accès. Comparer un `mtime_ns` coûte un `stat` ; relire ne se produit que
        lorsque le fichier a réellement changé."""
        if self._disk_mtime() == self._mtime:
            return
        try:
            self._db.close()
        except Exception:  # noqa: BLE001 — best-effort, on rouvre juste après
            pass
        self._open()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception as exc:  # noqa: BLE001 — best-effort à l'arrêt
            logger.warning("[gallery] fermeture en échec : %s", exc)

    # -- écriture --

    def add(self, url: str, prompt: str, prompt_source: str = "", lang: str = "en",
            width: int = 0, height: int = 0, seed: int = 0, origin: str = "web",
            comfy: dict | None = None) -> dict:
        """Insère une image. `comfy` ne porte l'origine ComfyUI que si le fichier
        y est resté (téléchargement `/view`) ; après un déplacement, il vaut None."""
        now = datetime.now()
        entry = {
            "id": _generate_id(),
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.isoformat(),
            "url": url,
            "prompt": prompt,
            "prompt_source": prompt_source,
            "lang": lang,
            "width": int(width),
            "height": int(height),
            "seed": int(seed),
            "origin": origin,
        }
        if comfy:
            entry.update({
                "comfy_filename": comfy.get("filename"),
                "comfy_subfolder": comfy.get("subfolder", ""),
                "comfy_type": comfy.get("type", "output"),
            })
        with self._lock:
            self._sync()
            self._table.insert(entry)
            self._mtime = self._disk_mtime()
        return entry

    def delete(self, entry_id: str) -> bool:
        """Retire le document, puis le fichier de `media/`, puis l'original ComfyUI
        s'il est connu. Idempotent : supprimer deux fois n'est pas une erreur."""
        with self._lock:
            self._sync()
            entry = self._table.get(Query().id == entry_id)
            if entry is None:
                return False
            self._table.remove(Query().id == entry_id)
            self._mtime = self._disk_mtime()

        _delete_media_file(entry.get("url", ""))
        _delete_comfy_source(entry.get("comfy_filename"),
                             entry.get("comfy_subfolder", ""),
                             entry.get("comfy_type", "output"))
        return True

    # -- lecture --

    def get(self, entry_id: str) -> dict | None:
        self._sync()
        return self._table.get(Query().id == entry_id)

    def get_by_date(self, date: str) -> list[dict]:
        self._sync()
        entries = self._table.search(Query().date == date)
        entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return entries

    def get_dates_with_counts(self) -> list[dict]:
        """`[{date, count}]`, du jour le plus récent au plus ancien."""
        self._sync()
        totals: dict[str, int] = {}
        for entry in self._table.all():
            totals[entry.get("date", "")] = totals.get(entry.get("date", ""), 0) + 1
        return [{"date": date, "count": count}
                for date, count in sorted(totals.items(), reverse=True) if date]

    def search(self, query: str, limit: int = 300) -> list[dict]:
        """Recherche locale sur les deux prompts, sans appeler Ollama.

        Les tokens sont cherchés dans `prompt` **et** `prompt_source` : le prompt
        français saisi par l'utilisateur est stocké à côté de sa traduction anglaise,
        donc une requête en français retrouve une image dont le prompt envoyé au
        modèle était anglais. Plusieurs mots = ET.
        """
        tokens = [t for t in _normalize(query).split() if t]
        if not tokens:
            return []
        self._sync()
        found = []
        for entry in self._table.all():
            haystack = _normalize(f"{entry.get('prompt', '')} "
                                  f"{entry.get('prompt_source', '')}")
            if all(token in haystack for token in tokens):
                found.append(entry)
        found.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return found[:limit]


#: Instance unique du processus. Le studio et le serveur MCP en ont chacun la leur,
#: et c'est le `mtime_ns` qui les tient synchronisées.
store = GalleryStore()
