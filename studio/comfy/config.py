"""Lecture des workflows : `workflows/<nom>/workflow_api.json` + `mapping.json`.

Le graphe et la table des correspondances sont deux fichiers séparés pour que le
premier reste exactement ce que ComfyUI exporte — modifiable en le rechargeant dans
ComfyUI — pendant que le second dit au studio où poser chaque paramètre.
"""

import json

from studio.config import COMFYUI_URL, WORKFLOWS_DIR  # noqa: F401  (ré-export)

DEFAULT_WORKFLOW = "zimage"


def _read(name: str, filename: str) -> dict:
    path = WORKFLOWS_DIR / name / filename
    if not path.exists():
        raise ValueError(f"Workflow introuvable : {path}")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_workflow(name: str = DEFAULT_WORKFLOW) -> dict:
    """Le graphe au format API, tel qu'il sera POSTé à ComfyUI."""
    return _read(name, "workflow_api.json")


def load_workflow_mapping(name: str = DEFAULT_WORKFLOW) -> dict:
    """La table `paramètre -> (node, champ)` utilisée par `build_workflow`."""
    return _read(name, "mapping.json")
