"""Le carnet resOS : l'API publique v1.2 (doc Postman du 23/07/2025, docs/RESOS.md).

Écrit sans clé : resOS n'a ni bac à sable ni compte de test, et son API est une option
payante. Tout a été développé contre `tests/faux_resos.py`, qui reproduit la doc. Les
points que la doc ne tranche pas sont traités du côté PRUDENT, et listés dans
docs/RESOS.md pour le premier vrai appel.

Trois règles, qui valent plus que le chemin heureux :

1. **Ne jamais annoncer ce qui n'est pas sûr.** Tout ce qui n'est pas une réponse
   exploitable — délai, réseau, 5xx, 429, clé refusée — lève `Injoignable`, et le
   modèle reçoit l'ordre de ne rien confirmer.
2. **Vérifier le créneau juste avant d'écrire.** On ignore ce que resOS répond à une
   réservation sur un créneau complet (question 5 de SCRUM-82) : s'il l'accepte en
   silence, le restaurant recevrait une demande qu'il ne peut pas honorer.
3. **Ne rendre à un appelant que SES réservations.** Le filtre `customQuery` de resOS
   n'a jamais été éprouvé : le numéro est revérifié ici, sur chaque réservation.

Pas de copie locale : resOS n'a pas de webhook, il reste la seule source de vérité et on
le lit au moment de l'appel.
"""
import asyncio
import os
import re
from typing import Optional
from urllib.parse import quote

import httpx

from .. import horloge
from . import Complet, Injoignable, Refus

# Au-delà, l'appelant entend un blanc et dit « allô ? ». Une réponse plus lente vaut
# une panne : on le dit au modèle plutôt que de laisser la ligne muette. Plafond de
# BOUT EN BOUT : le délai de httpx s'applique à chaque étape (connexion, envoi,
# lecture), et trois étapes lentes feraient douze secondes de silence.
DELAI_SECONDES = 4.0

# Champ exigé par resOS à la création. La doc ne dit pas si le restaurant peut imposer
# la sienne : à vérifier au premier vrai appel (docs/RESOS.md).
DUREE_MINUTES = 120

AUTEUR = "Assistante téléphonique"
NOTE_INTERNE = "Prise au téléphone par l'assistante."

# Statuts d'une réservation encore attendue au restaurant. Les autres — declined,
# canceled, arrived, seated, left, no_show — ne se modifient ni ne s'annulent plus.
_ACTIFS = ("request", "approved", "waitlist")

# Remplacé par les tests (httpx.ASGITransport vers le faux resOS). None = le réseau.
_transport: Optional[httpx.AsyncBaseTransport] = None


def url_api() -> str:
    return os.getenv("RESOS_API_URL", "https://api.resos.com/v1").strip().rstrip("/")


def cle_pour(tenant_id: int) -> Optional[str]:
    """La clé resOS d'un établissement, lue dans `RESOS_API_KEYS="3=clé,7=autre"`.

    Dans le `.env` et pas en base : elle ouvre TOUTES les données du restaurant, et la
    base part dans les sauvegardes. Jamais journalisée."""
    for couple in os.getenv("RESOS_API_KEYS", "").split(","):
        identifiant, _, cle = couple.partition("=")
        if identifiant.strip() == str(tenant_id) and cle.strip():
            return cle.strip()
    return None


def _chiffres(telephone) -> str:
    """« +33 6 12 34 56 78 » → « 33612345678 » : la forme du filtre resOS (indicatif en
    tête, sans le +), et la seule comparaison fiable entre deux écritures d'un numéro."""
    return re.sub(r"\D", "", str(telephone or ""))


def _plus_proches(libres: list[str], heure: str, combien: int = 3) -> list[str]:
    def minutes(hhmm: str) -> int:
        h, _, m = hhmm.partition(":")
        return int(h) * 60 + int(m or 0)

    try:
        cible = minutes(heure)
        retenus = sorted(libres, key=lambda h: abs(minutes(h) - cible))[:combien]
    except ValueError:
        retenus = libres[:combien]
    return sorted(retenus)


def _en_reservation(booking: dict) -> dict:
    invite = booking.get("guest") or {}
    return {
        "id": booking.get("_id"),
        "customer_name": invite.get("name"),
        "customer_phone": invite.get("phone"),
        "date": booking.get("date"),
        "time": booking.get("time"),
        "party_size": booking.get("people"),
        "notes": booking.get("comment") or None,
        "a_valider": booking.get("status") == "request",
    }


def _a_venir(booking: dict) -> bool:
    return (booking.get("status") in _ACTIFS
            and (booking.get("date") or "") >= horloge.aujourd_hui().isoformat())


class ConnecteurResos:
    def __init__(self, tenant):
        self.tenant = tenant
        self.cle = cle_pour(tenant.id)

    async def _requete(self, methode: str, chemin: str, *, params=None, corps=None):
        """La réponse JSON ; None pour une 404. Lève `Injoignable` ou `Refus`."""
        if not self.cle:
            raise Injoignable("aucune clé resOS pour cet établissement (RESOS_API_KEYS)")
        try:
            async with httpx.AsyncClient(base_url=url_api(), auth=(self.cle, ""),
                                         timeout=DELAI_SECONDES,
                                         transport=_transport) as client:
                reponse = await asyncio.wait_for(
                    client.request(methode, chemin, params=params, json=corps),
                    DELAI_SECONDES)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            # Une écriture coupée ici a peut-être abouti chez resOS : on préfère une
            # demande en double, que le restaurant voit, à une confirmation inventée.
            raise Injoignable(f"{methode} {chemin} : {type(exc).__name__}") from exc
        code = reponse.status_code
        if code == 404:
            return None
        if code in (401, 403):
            raise Injoignable(f"resOS refuse la clé de l'établissement {self.tenant.id} ({code})")
        if code == 429 or code >= 500:
            raise Injoignable(f"resOS a répondu {code}")
        if code >= 400:
            raise Refus(reponse.text[:200])
        try:
            return reponse.json()
        except ValueError as exc:
            raise Injoignable(f"réponse illisible de resOS ({code})") from exc

    async def _libres(self, date: str, couverts: int) -> list[str]:
        services = await self._requete("GET", "/bookingFlow/times",
                                       params={"date": date, "people": int(couverts)})
        libres = set()
        for service in services or []:
            libres.update(service.get("availableTimes") or [])
        return sorted(libres)

    async def disponibilite(self, date, heure, couverts) -> dict:
        libres = await self._libres(date, couverts)
        if heure in libres:
            return {"available": True}
        return {"available": False, "autres_horaires": _plus_proches(libres, heure)}

    async def creer(self, *, nom, date, heure, couverts, telephone, notes) -> dict:
        libres = await self._libres(date, couverts)
        if heure not in libres:
            raise Complet(_plus_proches(libres, heure))
        invite = {"name": nom,
                  # resOS n'écrit ni en français ni sans frais : pas de SMS ni d'e-mail
                  # de sa part. Le client reste prévenu par l'assistante (SCRUM-84).
                  "notificationSms": False, "notificationEmail": False}
        if telephone:
            invite["phone"] = telephone
        corps = {
            "date": date, "time": heure, "people": int(couverts),
            "duration": DUREE_MINUTES,
            # « À valider par le restaurant » : décision de Helmi, 25/09/2026.
            "status": "request",
            "source": "phone",
            "guest": invite,
            "note": NOTE_INTERNE,
            "noteAuthor": AUTEUR,
            "metadata": {"origine": "assistante-telephonique"},
        }
        if notes:
            corps["comment"] = notes
        identifiant = await self._requete("POST", "/bookings", corps=corps)
        if not isinstance(identifiant, str) or not identifiant:
            raise Injoignable("resOS n'a pas rendu d'identifiant de réservation")
        return {"id": identifiant, "customer_name": nom, "customer_phone": telephone,
                "date": date, "time": heure, "party_size": int(couverts),
                "notes": notes, "a_valider": True}

    async def retrouver(self, telephone, a_partir_de=None) -> list[dict]:
        numero = _chiffres(telephone)
        if not numero:
            return []
        debut = max(a_partir_de or "", horloge.aujourd_hui().isoformat())
        trouves = await self._requete("GET", "/bookings", params={
            "fromDateTime": debut, "customQuery": f'guest.phone:"{numero}"',
            "limit": 100, "sort": "dateTime:1"})
        return [_en_reservation(b) for b in trouves or []
                if isinstance(b, dict) and _a_venir(b) and b.get("date", "") >= debut
                and _chiffres((b.get("guest") or {}).get("phone")) == numero]

    async def _lire(self, reservation_id) -> Optional[dict]:
        # L'identifiant vient du modèle : échappé, il ne peut pas sortir de /bookings/.
        booking = await self._requete("GET", f"/bookings/{quote(str(reservation_id), safe='')}")
        return booking if isinstance(booking, dict) else None

    async def pour_appelant(self, reservation_id, telephone) -> Optional[dict]:
        numero = _chiffres(telephone)
        if not numero or not str(reservation_id or "").strip():
            return None
        booking = await self._lire(reservation_id)
        if (booking is None or not _a_venir(booking)
                or _chiffres((booking.get("guest") or {}).get("phone")) != numero):
            return None
        return _en_reservation(booking)

    async def modifier(self, reservation_id, champs: dict) -> dict:
        actuelle = await self._lire(reservation_id)
        if actuelle is None:
            raise Refus("réservation introuvable dans resOS")
        date = champs.get("date", actuelle.get("date"))
        heure = champs.get("time", actuelle.get("time"))
        couverts = champs.get("party_size", actuelle.get("people"))
        # Nouveau créneau seulement : la réservation occupe déjà le sien, et resOS la
        # compterait contre elle-même si on vérifiait un simple changement de couverts.
        if date != actuelle.get("date") or heure != actuelle.get("time"):
            libres = await self._libres(date, couverts)
            if heure not in libres:
                raise Complet(_plus_proches(libres, heure))
        corps = {cle: valeur for cle, valeur in
                 (("date", champs.get("date")), ("time", champs.get("time")),
                  ("people", champs.get("party_size"))) if valeur not in (None, "")}
        chemin = f"/bookings/{quote(str(reservation_id), safe='')}"
        if corps and await self._requete("PUT", chemin, corps=corps) is not True:
            raise Injoignable("resOS n'a pas confirmé la modification")
        if champs.get("notes"):
            # Le commentaire ne se modifie pas par PUT : on l'ajoute en note interne.
            await self._requete("POST", f"{chemin}/restaurantNote",
                                corps={"text": f"Demande du client : {champs['notes']}"})
        relue = await self._lire(reservation_id)
        if relue is None:
            raise Injoignable("réservation modifiée mais illisible dans resOS")
        return _en_reservation(relue)

    async def annuler(self, reservation_id) -> None:
        chemin = f"/bookings/{quote(str(reservation_id), safe='')}"
        if await self._requete("PUT", chemin, corps={"status": "canceled"}) is not True:
            raise Injoignable("resOS n'a pas confirmé l'annulation")

    async def dernier_nom(self, telephone) -> Optional[str]:
        # Volontairement vide : ce serait une requête réseau AVANT le décroché, sur un
        # service dont on n'a jamais mesuré la latence. À réévaluer avec une vraie clé.
        return None
