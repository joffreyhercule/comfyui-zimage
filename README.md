# comfyui-zimage

Générez des images sur votre ordinateur, sans compte, sans abonnement et sans envoyer
quoi que ce soit sur Internet. Vous décrivez ce que vous voulez, l'image apparaît.

Tout s'installe tout seul : vous lancez l'installateur, vous attendez, et c'est prêt.
Fonctionne sous **Windows, Linux et macOS**.

---

## Avant de commencer

| Il vous faut | Détail |
|---|---|
| **Python 3.10 ou plus récent** | [python.org/downloads](https://www.python.org/downloads/) — sous Windows, cochez « Add python.exe to PATH » |
| **~25 Go d'espace libre** | ComfyUI et les modèles d'image |
| **Une carte graphique** | NVIDIA (8 Go de mémoire vidéo ou plus) ou un Mac Apple Silicon. Sans carte, ça fonctionne mais chaque image prend plusieurs minutes |
| **Une bonne connexion** | environ 15 Go à télécharger, une seule fois |

---

## 1. Installer

Récupérez le projet, puis lancez l'installateur.

**Windows** — double-cliquez sur `install.bat`.

**Linux / macOS** — dans un terminal :

```bash
./install.sh
```

Comptez **20 minutes à une heure** selon votre connexion. L'installateur affiche sa
progression et vous dit ce qu'il fait.

> **Interrompu ?** Relancez-le simplement. Il reprend là où il s'était arrêté et ne
> retélécharge rien de ce qui est déjà là.

Il vous posera une seule question, à propos d'**Ollama** : un logiciel qui traduit
automatiquement vos descriptions en anglais avant de les envoyer au générateur
d'images, qui comprend beaucoup mieux cette langue. C'est **facultatif** — vous pouvez
répondre non et l'ajouter plus tard en relançant l'installateur. Sans lui, écrivez vos
descriptions en anglais pour de meilleurs résultats.

---

## 2. Lancer

**Windows** — double-cliquez sur `run.bat`.
**Linux / macOS** — `./run.sh`

Votre navigateur s'ouvre sur le studio. Laissez la fenêtre noire (la console) ouverte
tant que vous utilisez le studio : c'est elle qui fait tourner le moteur. Pour tout
arrêter, fermez-la ou appuyez sur `Ctrl+C`.

> **La toute première image prend 20 à 30 secondes** : le moteur charge ses modèles.
> Les suivantes arrivent en quelques secondes.

---

## 3. Créer des images

Décrivez votre image dans la barre du bas, puis **Générer**.

- **Largeur et hauteur** — indépendantes, de 256 à 2048 pixels. Elles sont ajustées
  au multiple de 16 le plus proche (une contrainte du générateur).
- **Variantes** — de 1 à 4 images d'affilée à partir de la même description. Chacune
  a sa barre de progression et sa croix pour l'annuler si elle ne vous plaît pas.
- **Langue** (en haut à droite) — traduit l'interface, et indique dans quelle langue
  vous écrivez vos descriptions. Treize langues disponibles.

Plus votre description est précise, meilleur est le résultat. Pensez au sujet, au
décor, à la lumière et au style : *« un chat noir endormi sur un canapé de velours
rouge, lumière douce du matin, photographie »* donne mieux que *« un chat »*.

### La galerie

Le bouton **☰** en haut à gauche ouvre vos images, rangées par jour. Cliquez sur un
jour pour le déplier, sur une image pour l'agrandir.

Une fois l'image agrandie, vous pouvez la **télécharger**, la **supprimer** (deux
clics, pour éviter l'accident), ou **réutiliser ses réglages** — la description et le
format reviennent dans la barre du bas, prêts pour une variation.

La recherche trouve vos images à partir des mots de votre description, dans votre
langue, même si l'image a été générée à partir d'une traduction anglaise.

Vos images sont enregistrées dans le dossier `media/` du projet, classées par date.
Rien n'est envoyé ailleurs.

---

## Si quelque chose ne va pas

**« Python 3.10+ est introuvable »** — installez Python depuis
[python.org](https://www.python.org/downloads/) en cochant bien « Add python.exe to
PATH », puis relancez l'installateur.

**Le navigateur ne s'ouvre pas** — allez à [http://127.0.0.1:8000](http://127.0.0.1:8000)
manuellement.

**« ComfyUI n'a pas répondu »** — le moteur n'a pas démarré. Le fichier
`logs/comfyui.log` en donne la raison. Le plus souvent : pas assez de mémoire vidéo,
ou une installation incomplète — relancer l'installateur répare.

**Les images sont très lentes, ou le studio plante en cours de génération** — votre
carte manque de mémoire. Ouvrez `config.ini` et remplacez la ligne `extra_args =`
par :

```ini
extra_args = --reserve-vram 2
```

**Mes descriptions ne sont pas traduites** — Ollama n'est pas installé ou pas démarré.
Ce n'est pas grave : écrivez en anglais, ou relancez l'installateur pour l'ajouter.

**Une erreur qui ne figure pas ici** — le dossier `logs/` contient les journaux du
moteur (`comfyui.log`) ; c'est là que se trouve l'explication.

---

## Aller plus loin

**Brancher un assistant IA.** Le fichier `mcp_server.py` permet à un assistant
compatible MCP (Claude Code, Claude Desktop) de générer des images pour vous. La
configuration à lui donner :

```json
{
  "mcpServers": {
    "comfyui-zimage": {
      "command": "<chemin du projet>/venv/Scripts/python.exe",
      "args": ["<chemin du projet>/mcp_server.py"]
    }
  }
}
```

Sous Linux et macOS, remplacez `venv/Scripts/python.exe` par `venv/bin/python`. Les
images ainsi créées apparaissent dans votre galerie.

**Découper une image sur fond transparent.** Le projet embarque de quoi générer un
personnage ou un objet puis le détourer proprement en PNG transparent : voir
[.claude/skills/asset-detoure/](.claude/skills/asset-detoure/).

**Régler le studio.** Le fichier `config.ini`, créé par l'installateur, contient les
chemins et les quelques réglages modifiables ; `config.ini.example` explique chaque
ligne.

---

## Ce que ce projet ne fait pas

Il ne fait qu'une chose : **créer des images à partir de texte**. Pas de retouche, pas
de vidéo, pas de modèles supplémentaires à gérer. C'est délibéré : un outil qui marche
dès la première minute, plutôt qu'un atelier complet à configurer.
