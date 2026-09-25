"""Notre propre carnet : `reservations.py` sur SQLite, exactement comme avant SCRUM-83.

Chaque accès passe par `db.hors_boucle` : on s'exécute dans la boucle d'événements qui
sert TOUS les appels en cours, et un verrou SQLite attendu ici ferait bégayer leur voix.
"""
from typing import Optional

from .. import db, reservations


def _entier(reservation_id) -> Optional[int]:
    try:
        return int(reservation_id)
    except (TypeError, ValueError):
        return None


class ConnecteurInterne:
    def __init__(self, tenant):
        self.tenant = tenant

    async def disponibilite(self, date: str, heure: str, couverts: int) -> dict:
        # Pas de capacité modélisée : les horaires sont appliqués en amont
        # (llm._creneau_refuse). On donne au modèle les couverts déjà pris.
        pris = await db.hors_boucle(reservations.count_for_slot, self.tenant.id, date, heure)
        return {"available": True, "covers_already_booked": pris}

    async def creer(self, *, nom, date, heure, couverts, telephone, notes) -> dict:
        ligne = await db.hors_boucle(
            reservations.create_reservation,
            tenant_id=self.tenant.id, customer_name=nom, date=date, time=heure,
            party_size=couverts, customer_phone=telephone, notes=notes)
        return {**ligne, "a_valider": False}

    async def retrouver(self, telephone, a_partir_de=None) -> list[dict]:
        return await db.hors_boucle(reservations.find_by_phone, self.tenant.id, telephone,
                                    a_partir_de=a_partir_de)

    async def pour_appelant(self, reservation_id, telephone) -> Optional[dict]:
        identifiant = _entier(reservation_id)
        if identifiant is None:
            return None
        return await db.hors_boucle(reservations.get_for_caller, identifiant,
                                    self.tenant.id, telephone)

    async def modifier(self, reservation_id, champs: dict) -> dict:
        return await db.hors_boucle(reservations.update_reservation,
                                    _entier(reservation_id), **champs)

    async def annuler(self, reservation_id) -> None:
        await db.hors_boucle(reservations.cancel_reservation, _entier(reservation_id))

    async def dernier_nom(self, telephone) -> Optional[str]:
        return await db.hors_boucle(reservations.dernier_nom, self.tenant.id, telephone)
