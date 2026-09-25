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
from typing import Optional, Protocol

INTERNE = "interne"
RESOS = "resos"
FOURNISSEURS = (INTERNE, RESOS)


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


def pour(tenant) -> Connecteur:
    if fournisseur(tenant) == RESOS:
        from .resos import ConnecteurResos

        return ConnecteurResos(tenant)
    from .interne import ConnecteurInterne

    return ConnecteurInterne(tenant)
