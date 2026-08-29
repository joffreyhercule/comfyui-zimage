"""Chroma-key generique -> fond transparent (PNG RGBA) + anti-halo (despill).

Usage:
    python chroma_key.py <mode> image1.png [image2.png ...]
    mode = green | blue | magenta   (couleur du fond a retirer)

La couleur du fond DOIT etre absente du sujet :
    - animaux/objets bruns/orange/blancs/roses  -> green  (#00FF00)
    - sujet contenant du vert (feuilles, etc.)   -> magenta ou blue
    - sujet contenant vert ET rose               -> blue   (#0000FF)

Le fichier est ecrase par sa version detouree. Pillow + numpy requis.
"""
import sys
import numpy as np
from PIL import Image, ImageFilter

T_HARD, T_SOFT = 40, 12          # seuils du score : >HARD = fond, SOFT..HARD = bord doux

def score(R, G, B, mode):
    if mode == "green":   return G - np.maximum(R, B)
    if mode == "blue":    return B - np.maximum(R, G)
    if mode == "magenta": return np.minimum(R, B) - G
    raise SystemExit("mode inconnu: " + mode + " (green|blue|magenta)")

def despill(R, G, B, s, mode):
    """Attenue la teinte du fond qui bave sur les bords gardes."""
    spill = s > 0
    if mode == "green":
        cap = np.maximum(R, B) + 12
        return R, np.where(spill, np.minimum(G, cap), G), B
    if mode == "blue":
        cap = np.maximum(R, G) + 12
        return R, G, np.where(spill, np.minimum(B, cap), B)
    cap = G + 12                 # magenta
    return np.where(spill, np.minimum(R, cap), R), G, np.where(spill, np.minimum(B, cap), B)

def key(path, mode):
    im = Image.open(path).convert("RGBA")
    arr = np.asarray(im).astype(np.float32)
    R, G, B = arr[..., 0], arr[..., 1], arr[..., 2]
    s = score(R, G, B, mode)

    alpha = np.full(R.shape, 255.0, np.float32)
    alpha[s > T_HARD] = 0
    soft = (s > T_SOFT) & (s <= T_HARD)
    alpha[soft] = 255 * (T_HARD - s[soft]) / (T_HARD - T_SOFT)

    Rn, Gn, Bn = despill(R, G, B, s, mode)

    # IMPORTANT : assembler RGB et alpha SEPAREMENT.
    # Ne JAMAIS faire Image.fromarray(tableau_4_canaux, "RGB") -> melange les octets
    # et detruit les couleurs (image qui parait grise / baveuse).
    rgb = np.ascontiguousarray(np.clip(np.stack([Rn, Gn, Bn], -1), 0, 255).astype(np.uint8))
    a8 = np.clip(alpha, 0, 255).astype(np.uint8)

    a_im = Image.fromarray(a8, "L")
    a_im = a_im.filter(ImageFilter.MinFilter(3))      # grignote 1px -> vire le liisere
    a_im = a_im.filter(ImageFilter.GaussianBlur(0.6)) # anti-aliasing du contour

    res = Image.fromarray(rgb, "RGB").convert("RGBA")
    res.putalpha(a_im)
    res.save(path)

    # controle qualite : % garde + saturation moyenne (proche de 0 = probleme couleur)
    op = rgb.reshape(-1, 3)[a8.reshape(-1) > 200]
    sat = float((op.max(1).astype(int) - op.min(1).astype(int)).mean()) if len(op) else 0
    mean = op.mean(0).round(0) if len(op) else "NA"
    h, w = R.shape
    print(f"{path} [{mode}]: {int((a8 > 10).sum())*100//(h*w)}% garde, "
          f"satMoy={sat:.0f}, meanRGB={mean}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    mode = sys.argv[1]
    for p in sys.argv[2:]:
        key(p, mode)
