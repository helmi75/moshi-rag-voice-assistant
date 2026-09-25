"""L'état du carnet resOS de chaque établissement (SCRUM-87).

Trois usages, une seule source :
- **le journal** des dernières requêtes (mémoire), que montre la page « Carnet resOS » ;
- **le coupe-circuit** : après un échec, plus aucun essai pendant `COUPE_CIRCUIT_SECONDES`.
  Sans lui, chaque outil du même appel attendrait à nouveau 4 s un service en panne —
  quatre blancs de suite pour un seul appelant ;
- **l'ardoise de supervision** (`supervision.noter`, en base) : l'état survit aux
  redémarrages, et le contrôle « Carnet resOS » le lit. On n'y écrit qu'aux
  CHANGEMENTS d'état, pas à chaque requête : un appel sain n'ajoute aucune écriture.

Et l'alerte au restaurateur, espacée : un e-mail par panne, pas un par tour de parole.
"""
import time
from collections import deque
from typing import Optional

from .. import db, horloge

JOURNAL_MAX = 50
COUPE_CIRCUIT_SECONDES = 60.0
ALERTE_ESPACEMENT_SECONDES = 30 * 60.0

_journaux: dict[int, deque] = {}
_dernier_echec: dict[int, float] = {}   # time.monotonic()
_echecs: dict[int, dict] = {}           # la panne en cours, telle qu'écrite sur l'ardoise
_sain_ecrit: set[int] = set()           # « ok » déjà écrit sur l'ardoise depuis le démarrage
_derniere_alerte: dict[int, float] = {}


def cle_ardoise(tenant_id: int) -> str:
    return f"carnet:{tenant_id}"


def journal(tenant_id: int) -> list[dict]:
    """Les dernières requêtes, la plus récente d'abord."""
    return list(_journaux.get(tenant_id, ()))


def en_coupure(tenant_id: int) -> Optional[float]:
    """Secondes écoulées depuis l'échec qui a ouvert le circuit, ou None s'il est fermé."""
    depuis = _dernier_echec.get(tenant_id)
    if depuis is None:
        return None
    ecoule = time.monotonic() - depuis
    return ecoule if ecoule < COUPE_CIRCUIT_SECONDES else None


async def noter(tenant_id: int, *, methode: str, chemin: str, code: Optional[int] = None,
                duree_ms: Optional[int] = None, erreur: Optional[str] = None,
                panne: bool = False) -> None:
    """Une requête vers resOS. `panne` : resOS n'a pas répondu de façon exploitable.
    Une erreur SANS panne (404, 422) est une réponse — resOS va bien."""
    _journaux.setdefault(tenant_id, deque(maxlen=JOURNAL_MAX)).appendleft({
        "instant": horloge.utc_iso(), "methode": methode, "chemin": chemin,
        "code": code, "duree_ms": duree_ms, "erreur": erreur, "panne": panne})
    from .. import supervision

    if panne:
        _dernier_echec[tenant_id] = time.monotonic()
        _sain_ecrit.discard(tenant_id)
        maintenant = horloge.utc_iso()
        en_cours = _echecs.setdefault(tenant_id, {"depuis": maintenant, "echecs": 0})
        en_cours.update({"etat": "echec", "dernier_echec": maintenant, "erreur": erreur,
                         "echecs": en_cours["echecs"] + 1})
        await db.hors_boucle(supervision.noter, cle_ardoise(tenant_id), dict(en_cours))
        return
    _dernier_echec.pop(tenant_id, None)
    _echecs.pop(tenant_id, None)
    if tenant_id not in _sain_ecrit:
        await db.hors_boucle(supervision.noter, cle_ardoise(tenant_id),
                             {"etat": "ok", "dernier_succes": horloge.utc_iso()})
        _sain_ecrit.add(tenant_id)


def alerte_due(tenant_id: int) -> bool:
    """Vrai au plus une fois par `ALERTE_ESPACEMENT_SECONDES` et par établissement."""
    maintenant = time.monotonic()
    derniere = _derniere_alerte.get(tenant_id)
    if derniere is not None and maintenant - derniere < ALERTE_ESPACEMENT_SECONDES:
        return False
    _derniere_alerte[tenant_id] = maintenant
    return True


def rouvrir(tenant_id: int) -> None:
    """Referme le coupe-circuit sans attendre (page « Carnet resOS », après une panne
    simulée) : l'historique et l'ardoise restent, seul le prochain essai est autorisé."""
    _dernier_echec.pop(tenant_id, None)


def reinitialiser() -> None:
    """Oublie tout : pour les tests, où chaque base jetable réutilise les mêmes id."""
    for registre in (_journaux, _dernier_echec, _echecs, _derniere_alerte):
        registre.clear()
    _sain_ecrit.clear()
