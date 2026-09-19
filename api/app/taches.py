"""Registre des tâches de fond : rien ne se lance sans être retenu, ni ne meurt en silence.

`asyncio.create_task(...)` sans garder la tâche est un piège que la documentation d'asyncio
signale elle-même : la boucle n'en garde qu'une référence faible, et une tâche que plus
personne ne tient peut être ramassée avant d'avoir fini. Le projet en comptait sept —
rendu de l'accueil depuis l'admin, préchauffage du LLM, réveil du GPU… Symptôme typique :
un accueil « jamais rendu », sans erreur nulle part.

Second défaut du même motif : l'exception d'une tâche abandonnée n'est journalisée que
quand le ramasse-miettes passe, si jamais. Ici, chaque fin de tâche est observée : une
exception (autre qu'une annulation) est écrite dans le journal, avec le nom de la tâche.
"""
import asyncio
from typing import Coroutine

from loguru import logger

_taches: set[asyncio.Task] = set()


def lancer(coro: Coroutine, *, nom: str) -> asyncio.Task:
    """Lance `coro` en tâche de fond, la retient, et journalise sa fin anormale.

    Exige une boucle en cours : appelée hors boucle, l'erreur est immédiate et lisible
    (et la coroutine est refermée) plutôt qu'une tâche qui ne s'exécute jamais."""
    try:
        boucle = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        raise RuntimeError(f"taches.lancer({nom!r}) appelée hors d'une boucle d'événements")
    tache = boucle.create_task(coro, name=nom)
    _taches.add(tache)
    tache.add_done_callback(_terminee)
    return tache


def _terminee(tache: asyncio.Task) -> None:
    _taches.discard(tache)
    if tache.cancelled():
        return
    exc = tache.exception()
    if exc is not None:
        logger.warning(f"tâche de fond « {tache.get_name()} » terminée en erreur : "
                       f"{type(exc).__name__}: {exc}")


def en_cours() -> int:
    """Combien de tâches sont retenues en ce moment (supervision, tests)."""
    return len(_taches)


def retenues() -> list[asyncio.Task]:
    """Instantané des tâches retenues, pour les tests et le diagnostic."""
    return list(_taches)


async def arreter_tout() -> None:
    """Annule et attend toutes les tâches retenues — à l'arrêt du service.

    Sans ça, l'arrêt traîne, et un service qui ne sait pas s'arrêter finit tué au
    signal 9, en pleine écriture SQLite."""
    taches = list(_taches)
    for tache in taches:
        if not tache.done():
            tache.cancel()
    for tache in taches:
        try:
            await tache
        except (asyncio.CancelledError, Exception):
            pass
    _taches.clear()
