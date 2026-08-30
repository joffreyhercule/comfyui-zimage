"""Un port est-il utilisable, et lequel prendre quand il ne l'est pas.

Windows réserve des plages entières de ports — Hyper-V, WSL, les conteneurs les
prennent au démarrage — et s'y lier échoue en **WinError 10013**, une erreur de
*permission*, pas d'occupation. C'est exactement ce qui arrive au port 8000, l'un des
plus disputés de la plateforme. `netsh interface ipv4 show excludedportrange
protocol=tcp` liste ces plages.

D'où deux verdicts distincts plutôt qu'un booléen : un port refusé ne marchera jamais,
alors qu'un port occupé appartient peut-être au composant qu'on s'apprêtait à lancer —
un ComfyUI déjà en route, le studio en train de tourner. Les confondre ferait déplacer
un port qui était le bon.

Module volontairement sans dépendance : `install.py` l'importe avant que les paquets
du projet ne soient installés.
"""

from __future__ import annotations

import socket

FREE = "free"
BUSY = "busy"
FORBIDDEN = "forbidden"


def candidates(preferred: int):
    """Les ports à essayer, dans l'ordre, à partir de celui qu'on voulait.

    D'abord les voisins immédiats, pour rester près du numéro que l'utilisateur a lu
    dans la documentation. Puis des bonds de 100 : les plages réservées de Windows font
    justement cent ports de large et se suivent souvent — 9158-9257, 9258-9357,
    9358-9457 sur une machine ordinaire — si bien qu'un balayage linéaire de vingt ports
    n'en sortirait jamais. Les bonds gardent la même famille de numéros : 8388, 8488,
    8588, ce qui se lit et se retient.
    """
    yield from range(preferred, preferred + 10)
    for jump in (100, 200, 300, 400, 500):
        yield from range(preferred + jump, preferred + jump + 3)


def bind_status(port: int, host: str = "127.0.0.1") -> str:
    """`FREE`, `BUSY` ou `FORBIDDEN` pour `host:port`.

    Sans `SO_REUSEADDR` : on veut la réponse que le vrai serveur obtiendra, pas une
    version optimiste de celle-ci.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except PermissionError:  # WSAEACCES / EACCES : plage réservée, ou port < 1024
            return FORBIDDEN
        except OSError:  # déjà pris, ou adresse indisponible
            return BUSY
    return FREE


def pick_port(preferred: int, is_mine=None, host: str = "127.0.0.1") -> int:
    """Le port voulu s'il est utilisable, sinon le premier qui l'est au-dessus.

    `is_mine(port)` répond « ce port est déjà tenu par le composant à qui je le
    destine » — un ComfyUI qui répond sur `/system_stats`, un studio qui répond sur
    `/api/config`. Un port occupé par le bon serveur est le bon port : on le garde.
    Sans cette question, on ne garde que les ports libres.

    Rend `preferred` si rien ne convient : mieux vaut échouer sur le port annoncé,
    avec un message clair au lancement, que sur un numéro sorti d'on ne sait où.
    """
    for candidate in candidates(preferred):
        status = bind_status(candidate, host)
        if status == FREE:
            return candidate
        if status == BUSY and is_mine is not None and is_mine(candidate):
            return candidate
    return preferred
