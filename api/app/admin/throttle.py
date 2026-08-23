"""Limitation des tentatives de connexion, et trace des échecs.

L'admin est sur l'Internet public et des sondes automatisées y sont déjà passées.
bcrypt freine naturellement (~150 ms par essai), mais rien n'empêchait un grignotage
prolongé, et surtout **rien n'en gardait la trace** : après l'exposition du port 8000
en août, il a été impossible de dire si quelqu'un avait tenté sa chance.

Compteur en mémoire, volontairement : pas de dépendance, pas de table, et une remise à
zéro au redémarrage. Ce n'est pas une défense contre un attaquant distribué — c'est un
frein contre le grignotage depuis une poignée d'adresses, et un journal exploitable.
"""
import os
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import Request

# Au-delà de MAX échecs sur FENETRE secondes depuis la même adresse, on refuse sans
# même vérifier le mot de passe. Large exprès : un restaurateur qui se trompe trois
# fois ne doit pas être bloqué.
MAX_ECHECS = int(os.getenv("LOGIN_MAX_ECHECS", "10"))
FENETRE = int(os.getenv("LOGIN_FENETRE_SECONDES", "300"))

_echecs: dict[str, Deque[float]] = defaultdict(deque)


def adresse(request: Request) -> str:
    """Adresse réelle de l'appelant, derrière Caddy.

    ⚠️ On prend la DERNIÈRE entrée de X-Forwarded-For, pas la première : un client peut
    envoyer son propre en-tête, et Caddy AJOUTE l'adresse du pair à la suite. La
    dernière valeur est donc la seule que le client ne contrôle pas."""
    transmis = request.headers.get("x-forwarded-for", "")
    if transmis:
        return transmis.split(",")[-1].strip()
    return request.client.host if request.client else "inconnue"


def _purger(file: Deque[float], maintenant: float) -> None:
    while file and maintenant - file[0] > FENETRE:
        file.popleft()


def bloque(request: Request) -> int:
    """Secondes à attendre si l'adresse est bloquée, 0 sinon."""
    maintenant = time.monotonic()
    file = _echecs[adresse(request)]
    _purger(file, maintenant)
    if len(file) < MAX_ECHECS:
        return 0
    return max(1, int(FENETRE - (maintenant - file[0])))


def enregistrer_echec(request: Request) -> int:
    """Compte un échec. Renvoie le nombre d'échecs récents pour cette adresse."""
    maintenant = time.monotonic()
    file = _echecs[adresse(request)]
    _purger(file, maintenant)
    file.append(maintenant)
    return len(file)


def reinitialiser(request: Request) -> None:
    """Connexion réussie : on efface l'ardoise de cette adresse."""
    _echecs.pop(adresse(request), None)


def vider() -> None:
    """Remise à zéro complète — réservée aux tests."""
    _echecs.clear()
