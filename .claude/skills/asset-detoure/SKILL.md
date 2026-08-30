---
name: asset-detoure
description: Générer une image (personnage, objet, icône) via le MCP ComfyUI puis la détourer proprement en PNG transparent. À utiliser dès qu'on a besoin d'un sprite/asset à fond transparent pour un jeu ou une page (créatures, boutons, icônes…). Couvre le choix de la couleur chroma, la génération, le téléchargement et le chroma-key Python.
---

# Générer une image bien détourée (fond transparent)

Méthode éprouvée pour obtenir un asset PNG transparent **net et en couleurs**, sans
outil de fond-vert externe (juste ComfyUI + Pillow/numpy, tous deux dans le venv du
projet).

## Principe

On **ne demande pas** la transparence au modèle (Z-Image ne la gère pas). On génère
le sujet sur un **fond chroma uni et vif**, puis on retire cette couleur en Python.
La règle d'or : **la couleur du fond doit être ABSENTE du sujet.**

## Étape 1 — Choisir la couleur chroma selon le sujet

| Le sujet contient…              | Fond à utiliser | Hex        |
|---------------------------------|-----------------|------------|
| brun / orange / blanc / rose    | **green**       | `#00FF00`  |
| du vert (feuilles, bouton vert) | **magenta**     | `#FF00FF`  |
| du vert ET du rose/magenta      | **blue**        | `#0000FF`  |

⚠️ Le **rouge vif** est un mauvais choix dès qu'il y a de l'orange/roux (renard,
écureuil) : il les mange. Le **magenta** se confond avec les joues roses kawaii →
préférer **green** pour les animaux. Choisir **par asset**, pas une couleur unique.

## Étape 2 — Générer avec ComfyUI

`mcp__comfyui-zimage__generate_image` avec un prompt qui :
- décrit le sujet + style (ex. `kawaii chibi, big eyes, rosy cheeks, bright vibrant
  saturated colors, glossy, thick clean outlines, children's book illustration`) ;
- impose le fond : `isolated on a solid flat pure bright green chroma background
  #00FF00, no scenery, no shadow, no text`.

Les dimensions sont **deux paramètres indépendants**, arrondis au multiple de 16 et
bornés à 2048 côté serveur :

```
generate_image(prompt="…", width=1024, height=1024, seed=42)   # sprite carré
generate_image(prompt="…", width=1536, height=640)             # bannière
```

Réutiliser le même `seed` + les mêmes mots de style entre assets pour une **série
cohérente**. Pour « plus coloré / kawaii » : `bright vibrant saturated colors, glossy,
huge sparkling eyes, rosy pink cheeks`.

## Étape 3 — Télécharger

Le résultat fournit une ligne `Download URL:` (HTTP absolu). La récupérer :

```bash
curl -s -o assets/creature_1.png "http://127.0.0.1:8388/media/.../img_xxx.png"
```

Le studio doit tourner pour que cette URL réponde. S'il est arrêté, l'image existe
quand même sur le disque, sous `media/<AAAA-MM-JJ>/` du projet.

## Étape 4 — Détourer (chroma-key)

Le script `chroma_key.py` (à côté de ce fichier) fait le keying + anti-halo (despill)
+ adoucissement du contour. Il **écrase** le fichier par sa version transparente.

```bash
python "<chemin>/chroma_key.py" green creature_1.png creature_2.png   # animaux
python "<chemin>/chroma_key.py" magenta play.png                       # bouton vert
python "<chemin>/chroma_key.py" blue header.png                        # bannière (vert+rose)
```

Sortie de contrôle par fichier : `% gardé`, `satMoy`, `meanRGB`.
- `satMoy` ≈ 0 → souci de couleur (revoir étape 5).
- `meanRGB` doit correspondre à la teinte attendue (renard ≈ `[215,153,104]` orangé).

## Étape 5 — Pièges à connaître

- **NE JAMAIS** reconstruire l'image avec `Image.fromarray(tableau_4_canaux, "RGB")` :
  ça mélange les octets RGBA et **détruit les couleurs** (image qui paraît grise/baveuse).
  Toujours assembler **RGB et alpha séparément** (le script le fait déjà).
- L'**aperçu de l'outil Read peut désaturer** l'image : pour juger les vraies couleurs,
  composer le PNG sur un fond uni avec Pillow et lire ce composite, ou se fier à `meanRGB`.
- Si le détourage laisse du fond : monter `T_HARD` ; s'il mange le sujet : le baisser
  (défaut 40). Le `despill` cape la teinte de fond résiduelle sur les bords.
- **Hitbox pixel-perfect** côté navigateur : lire l'alpha via `<canvas>.getImageData`
  est **bloqué en `file://`** (canvas « teinté »). Prévoir un repli sur la bounding box ;
  le test au pixel ne marche qu'une fois servi en HTTP (`python -m http.server`).

## Vérifier visuellement

```python
from PIL import Image
files = ["creature_1.png", "creature_2.png"]
cell = 256; cols = 4; rows = (len(files)+cols-1)//cols
s = Image.new("RGB", (cols*cell, rows*cell), (30, 90, 55))  # fond vert pour repérer un halo
for i, f in enumerate(files):
    im = Image.open(f).convert("RGBA"); im.thumbnail((cell-16, cell-16))
    s.paste(im, ((i%cols)*cell+(cell-im.width)//2, (i//cols)*cell+(cell-im.height)//2), im)
s.save("_preview.png")
```
