"""Où se trouvent l'interpréteur et le `main.py` d'une installation ComfyUI.

Deux dispositions existent — le portable Windows (`python_embeded/` + `ComfyUI/`)
et le clone git (`venv/` + `main.py` à la racine) — et le code ne branche jamais
sur `sys.platform` pour les distinguer : on teste les deux et on rend celle qui
existe. Un portable copié sur un partage, ou une installation source sous Windows,
marchent donc sans cas particulier.

Module volontairement sans dépendance : `install.py` l'importe avant même que les
paquets du projet soient installés.
"""

from pathlib import Path

# (interpréteur, main.py) relatifs à la racine de l'installation, dans l'ordre
# d'essai. Chaque interpréteur est décliné en .exe / sans extension : c'est le
# seul endroit du projet où Windows et Unix se croisent.
_LAYOUTS = (
    (("python_embeded/python.exe", "python_embeded/python"), "ComfyUI/main.py"),
    (("venv/Scripts/python.exe", "venv/bin/python"), "main.py"),
    ((".venv/Scripts/python.exe", ".venv/bin/python"), "main.py"),
)


def resolve_comfy_layout(root) -> tuple[Path, Path, Path] | None:
    """Rend `(python, main_py, cwd)` pour l'installation ComfyUI à `root`, ou None.

    `cwd` est le dossier de `main.py` : ComfyUI y cherche `models/`, `output/` et
    ses extensions, donc c'est le répertoire de travail à lui donner au lancement.
    """
    if not root:
        return None
    root = Path(root).expanduser()
    if not root.is_dir():
        return None

    for interpreters, main_rel in _LAYOUTS:
        main_py = root / main_rel
        if not main_py.is_file():
            continue
        for interp_rel in interpreters:
            python = root / interp_rel
            if python.is_file():
                return python, main_py, main_py.parent
    return None


def comfy_models_dir(root) -> Path | None:
    """Dossier `models/` de l'installation — c'est là que vont les checkpoints.

    Passe par `resolve_comfy_layout` plutôt que de deviner : dans le portable il
    est sous `ComfyUI/`, dans un clone à la racine.
    """
    layout = resolve_comfy_layout(root)
    return layout[2] / "models" if layout else None
