"""Application FastAPI : neuf routes, un WebSocket, deux dossiers servis.

Le WebSocket est un canal de diffusion global : tous les événements partent à tous
les clients connectés, et c'est le front qui trie sur `conversation_id`. Pour un
studio personnel, c'est plus simple qu'un routage par session, et cela permet à un
second onglet de suivre une génération lancée depuis le premier.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from studio import gallery, generation, runner, translate
from studio.config import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MAX_LOT,
    MAX_SIDE,
    MEDIA_DIR,
    MIN_SIDE,
    SIDE_STEP,
    STATIC_DIR,
    snap_side,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_ws_clients: set[WebSocket] = set()


async def _broadcast(event: dict) -> None:
    """Diffuse un événement, en élaguant les sockets morts.

    Un client fermé sans handshake reste dans l'ensemble jusqu'à ce qu'on tente de
    lui écrire : c'est cet envoi qui le détecte, donc c'est ici qu'on le retire.
    """
    dead = set()
    for client in _ws_clients:
        try:
            await client.send_json(event)
        except Exception:  # noqa: BLE001 — socket mort, rien à faire d'autre
            dead.add(client)
    _ws_clients.difference_update(dead)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    gallery.store.close()


app = FastAPI(title="comfyui-zimage", version="1.0.0", lifespan=_lifespan)

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Les navigateurs servent volontiers un `app.js` périmé pendant des heures ;
    sur un studio local, c'est toujours une régression, jamais un gain.

    La page d'accueil est logée à la même enseigne : elle n'est pas plus versionnée
    que les scripts, et un index.html en cache annule une modification du HTML aussi
    sûrement qu'un app.js en cache. Les médias, eux, gardent leur cache : leur URL
    contient un identifiant unique, ils ne changent jamais.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config():
    """Tout ce dont le front a besoin pour se construire : la liste des langues, les
    bornes, les défauts. Dupliquer ces valeurs en JavaScript, c'est se garantir
    qu'elles divergeront."""
    return {
        "languages": translate.NATIVE_NAMES,
        "rtl_languages": sorted(translate.RTL_LANGUAGES),
        "translation_available": translate.is_available(),
        "default_width": DEFAULT_WIDTH,
        "default_height": DEFAULT_HEIGHT,
        "min_side": MIN_SIDE,
        "max_side": MAX_SIDE,
        "side_step": SIDE_STEP,
        "max_lot": MAX_LOT,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            # Rien n'est attendu du client : la réception ne sert qu'à détecter la
            # fermeture. Le front, lui, se reconnecte tout seul.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        _ws_clients.discard(ws)


@app.post("/api/generate")
async def generate(prompt: str = Form(...), lang: str = Form("en"),
                   width: int = Form(DEFAULT_WIDTH), height: int = Form(DEFAULT_HEIGHT),
                   lot: int = Form(1), seed: str = Form("")):
    """Lance un lot en tâche de fond et rend immédiatement son `conversation_id`.

    La réponse ne porte aucune image : tout arrive par le WebSocket, y compris les
    échecs. Le seed voyage en texte parce que « vide » (tirer au hasard) et « 0 »
    (un seed parfaitement valide) doivent rester distincts.
    """
    conversation_id = uuid.uuid4().hex[:12]
    seed_value = None
    if seed.strip():
        try:
            seed_value = int(seed.strip())
        except ValueError:
            raise HTTPException(400, "Le seed doit être un entier")

    asyncio.create_task(runner.run_lot(
        prompt=prompt, lang=lang, width=snap_side(width), height=snap_side(height),
        lot=lot, seed=seed_value, conversation_id=conversation_id,
        on_event=lambda event: _broadcast({**event, "conversation_id": conversation_id}),
    ))
    return {"conversation_id": conversation_id}


@app.post("/api/generate/cancel")
async def cancel_generation(conversation_id: str = Form(...), item_index: int = Form(...)):
    """Annule une image, qu'elle soit en cours de rendu ou en attente de son tour."""
    return await generation.cancel_slot(conversation_id, item_index)


# --- Galerie ---
# Les routes littérales sont déclarées avant `/{entry_id}` : sans cela, `dates`
# serait lu comme un identifiant.

@app.get("/api/gallery/dates")
async def gallery_dates():
    return gallery.store.get_dates_with_counts()


@app.get("/api/gallery/search")
async def gallery_search(q: str = ""):
    """Recherche locale, sans Ollama : les deux prompts sont stockés côte à côte."""
    return gallery.store.search(q)


@app.get("/api/gallery")
async def gallery_by_date(date: str = ""):
    if not date:
        return []
    return gallery.store.get_by_date(date)


@app.delete("/api/gallery/{entry_id}")
async def gallery_delete(entry_id: str):
    """Supprime le document, le fichier de `media/`, et l'original ComfyUI s'il
    existe encore. Un identifiant inconnu rend `deleted: false`, pas une erreur :
    supprimer deux fois n'est pas un échec."""
    return JSONResponse({"deleted": gallery.store.delete(entry_id)})
