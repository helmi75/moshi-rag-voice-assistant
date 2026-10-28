"""Alertes poussées sur Telegram (ASSISTANTE-161).

Jusqu'ici l'alerte de panne était l'e-mail d'un workflow GitHub, qui arrive jusqu'à
15 minutes après la panne. Ici le message part à la seconde où l'application la constate.

Trois règles :

1. **Sans `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`, rien ne part et rien ne casse.**
2. **Jamais sur le chemin de l'appel.** L'envoi tourne dans un fil daemon : on peut
   appeler d'une route synchrone, de la boucle d'événements ou d'un fil de la base sans
   rien bloquer. Il ne lève jamais.
3. **Le jeton est dans l'URL de l'API Telegram.** Un échec se journalise par le nom de
   l'exception et le code HTTP, jamais par son message (httpx y recopie l'URL).

Une seule alerte par épisode : `alerter` n'envoie que si l'épisode n'est pas déjà ouvert,
`retablir` que s'il l'était. Rien n'est écrit sur disque : un redémarrage de
l'application peut ré-alerter une panne qui dure, ce qui est le bon défaut.
Ne jamais y mettre le numéro d'un appelant ni une transcription (docs/RGPD.md).
"""
import os
import threading
import time
from threading import Thread
from typing import Optional

import httpx
from loguru import logger

_ouverts: dict[str, Optional[float]] = {}   # clé d'épisode → échéance (monotonic) ou None
_derniers: dict[str, float] = {}            # clé d'épisode → dernière alerte (monotonic)
# Une même clé ne réalerte pas avant ce délai, même si son épisode s'est refermé : une panne
# qui dure (la mémoire du renvoi expire toutes les 3 min, l'appel suivant la reconstate) ou
# un contrôle qui oscille autour de son seuil ne font pas une rafale (relecture d'ASSISTANTE-161).
REPRISE_SECONDES = 15 * 60
_verrou = threading.Lock()


def actif() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
                and os.getenv("TELEGRAM_CHAT_ID", "").strip())


def _envoyer(texte: str) -> None:
    jeton = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        reponse = httpx.post(
            f"https://api.telegram.org/bot{jeton}/sendMessage",
            json={"chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(), "text": texte},
            timeout=10,
        )
        reponse.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(f"[telegram] message non envoyé (HTTP {exc.response.status_code})")
    except Exception as exc:  # jamais le message : il contient l'URL, donc le jeton
        logger.warning(f"[telegram] message non envoyé ({type(exc).__name__})")


def expedier(texte: str) -> bool:
    """Envoie un message sans bloquer l'appelant. Faux si le module est inactif."""
    if not actif():
        return False
    try:
        Thread(target=_envoyer, args=(texte,), daemon=True, name="telegram").start()
    except Exception as exc:
        logger.warning(f"[telegram] fil non lancé ({type(exc).__name__})")
        return False
    return True


def alerter(cle: str, texte: str, valable_secondes: Optional[float] = None) -> bool:
    """Alerte une fois par épisode. `valable_secondes` referme l'épisode de lui-même
    (une panne qui sort de la mémoire du renvoi n'a pas de « retour à la normale »)."""
    if not actif():
        return False
    maintenant = time.monotonic()
    with _verrou:
        if cle in _ouverts:
            echeance = _ouverts[cle]
            if echeance is None or echeance > maintenant:
                return False
        if cle in _derniers and maintenant - _derniers[cle] < REPRISE_SECONDES:
            return False
        _ouverts[cle] = maintenant + valable_secondes if valable_secondes else None
        _derniers[cle] = maintenant
    return expedier(texte)


def retablir(cle: str, texte: str) -> bool:
    """Annonce le retour à la normale, seulement si l'épisode était ouvert."""
    if not actif():
        return False
    maintenant = time.monotonic()
    with _verrou:
        if cle not in _ouverts:
            return False
        echeance = _ouverts.pop(cle)
    if echeance is not None and echeance <= maintenant:
        return False   # l'épisode s'était déjà refermé de lui-même
    return expedier(texte)


def reinitialiser() -> None:
    with _verrou:
        _ouverts.clear()
        _derniers.clear()
