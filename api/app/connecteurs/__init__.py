"""Le carnet de réservations d'un établissement : le nôtre, ou celui de resOS (SCRUM-83).

`llm.run_tool` reste le SEUL endroit où un outil touche aux réservations
(ARCHITECTURE.md). Il ne sait plus où elles partent : il demande `pour(tenant)` et
parle à l'interface `Connecteur`. Le journal de l'appel (transcription, latences,
coût, enregistrement) reste chez nous quel que soit le carnet : c'est notre métier,
pas celui de resOS.

Une réservation circule sous la forme de la ligne de `reservations.py` — `id`,
`customer_name`, `customer_phone`, `date`, `time`, `party_size`, `notes` — plus
`a_valider` : vrai quand le restaurant doit encore l'accepter (resOS, statut
`request`). C'est cette forme que lisent les notifications et le modèle.
"""
import dataclasses
import json
import time
from typing import Optional, Protocol

from loguru import logger

INTERNE = "interne"
RESOS = "resos"
# Le faux resOS de l'établissement, dans l'application (SCRUM-93) : pour tester au
# téléphone « comme si c'était resOS », sans clé ni abonnement.
RESOS_DEMO = "resos_demo"
FOURNISSEURS = (INTERNE, RESOS, RESOS_DEMO)


class Injoignable(Exception):
    """Le carnet n'a pas répondu de façon exploitable : réseau, délai, erreur serveur,
    clé refusée ou absente. Rien n'est sûr — ni écrit, ni lu. L'assistante ne doit
    surtout pas annoncer une réservation enregistrée."""


class Refus(Exception):
    """Le carnet a lu la demande et l'a rejetée (requête invalide, opération interdite).
    Contrairement à `Injoignable`, on sait que rien n'a été écrit."""


class Complet(Exception):
    """Le créneau demandé n'est pas libre. `autres_horaires` : les plus proches qui le
    sont, le même jour, pour que l'assistante propose au lieu de refuser sèchement."""

    def __init__(self, autres_horaires: list[str]):
        super().__init__("créneau complet")
        self.autres_horaires = autres_horaires


class Connecteur(Protocol):
    async def disponibilite(self, date: str, heure: str, couverts: int) -> dict:
        """{"available": bool, …} — le contrat que lit le modèle."""

    async def creer(self, *, nom: str, date: str, heure: str, couverts: int,
                    telephone: Optional[str], notes: Optional[str]) -> dict: ...

    async def retrouver(self, telephone: str,
                        a_partir_de: Optional[str] = None) -> list[dict]: ...

    async def pour_appelant(self, reservation_id, telephone: str) -> Optional[dict]:
        """La réservation SI elle appartient à ce numéro et reste à venir, sinon None."""

    async def modifier(self, reservation_id, champs: dict) -> dict: ...

    async def annuler(self, reservation_id) -> None: ...

    async def dernier_nom(self, telephone: str) -> Optional[str]: ...


def fournisseur(tenant) -> str:
    """Le carnet de l'établissement. Absent ou inconnu = le nôtre, comme avant SCRUM-83 :
    le parc existant n'a rien à configurer."""
    valeur = (getattr(tenant, "booking_provider", None) or "").strip().lower()
    return valeur if valeur in FOURNISSEURS else INTERNE


def est_resos(tenant) -> bool:
    return fournisseur(tenant) in (RESOS, RESOS_DEMO)


def pour(tenant) -> Connecteur:
    if fournisseur(tenant) == RESOS:
        from .resos import ConnecteurResos

        return ConnecteurResos(tenant)
    if fournisseur(tenant) == RESOS_DEMO:
        from .resos import ConnecteurResos

        return ConnecteurResos(tenant, bac_a_sable=True)
    from .interne import ConnecteurInterne

    return ConnecteurInterne(tenant)


# --- Horaires du carnet (SCRUM-86) --------------------------------------------
# Le prompt part à CHAQUE tour de parole : il est hors de question d'interroger resOS à
# chaque fois, ni même au décroché (ce serait du silence avant « Bonjour »). On sert une
# copie en mémoire, rafraîchie en tâche de fond quand elle a vieilli.

HORAIRES_CACHE_SECONDES = 600.0
_horaires: dict[int, tuple[float, str]] = {}   # établissement → (time.monotonic(), JSON)
_rafraichissements: set[int] = set()


def horaires_en_cache(tenant) -> Optional[str]:
    entree = _horaires.get(tenant.id)
    return entree[1] if entree else None


async def rafraichir_horaires(tenant) -> Optional[str]:
    """Relit les horaires dans le carnet. En cas d'échec, l'ancienne copie reste servie :
    des horaires d'il y a une heure valent mieux qu'aucun."""
    try:
        horaires = await pour(tenant).horaires()
    except (Injoignable, Refus) as exc:
        logger.warning(f"horaires resOS de l'établissement {tenant.id} non relus : {exc}")
        return horaires_en_cache(tenant)
    finally:
        _rafraichissements.discard(tenant.id)
    brut = json.dumps(horaires, ensure_ascii=False)
    _horaires[tenant.id] = (time.monotonic(), brut)
    return brut


def avec_horaires_du_carnet(tenant):
    """L'établissement tel que le voient le prompt et `run_tool` : pour un carnet resOS,
    ses horaires d'ouverture sont ceux de resOS — c'est resOS qui fait foi, pas une
    saisie chez nous qui divergerait le jour des horaires d'été.

    Ne fait JAMAIS attendre : copie en mémoire, ou l'établissement tel quel si resOS n'a
    pas encore été lu (le créneau reste vérifié auprès de resOS avant chaque écriture)."""
    if not est_resos(tenant):
        return tenant
    entree = _horaires.get(tenant.id)
    if entree is None or time.monotonic() - entree[0] > HORAIRES_CACHE_SECONDES:
        _rafraichir_en_fond(tenant)
    if entree is None:
        return tenant
    return dataclasses.replace(tenant, opening_hours=entree[1])


def _rafraichir_en_fond(tenant) -> None:
    if tenant.id in _rafraichissements:
        return
    from .. import taches

    try:
        taches.lancer(rafraichir_horaires(tenant), nom=f"horaires resOS de l'établissement {tenant.id}")
        _rafraichissements.add(tenant.id)
    except RuntimeError:
        pass  # hors boucle d'événements (script, test synchrone) : rien à rafraîchir


def oublier_horaires() -> None:
    """Pour les tests."""
    _horaires.clear()
    _rafraichissements.clear()


# --- Panne du carnet (SCRUM-87) ---------------------------------------------------

def signaler_panne(tenant, outil: str, erreur: str, call_id: Optional[int] = None) -> None:
    """Le restaurateur l'apprend TOUT DE SUITE, par e-mail — pas le lendemain dans un
    tableau de bord. Un e-mail par panne, pas un par tour de parole."""
    from .. import notifications
    from . import sante

    if sante.alerte_due(tenant.id):
        notifications.planifier(tenant, "carnet_injoignable",
                                {"outil": outil, "erreur": erreur, "appel_id": call_id})
