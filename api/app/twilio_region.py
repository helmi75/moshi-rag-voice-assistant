"""La région où Twilio traite un appel : États-Unis (`us1`, son défaut) ou Irlande (`ie1`).

Pourquoi s'en soucier : un numéro français traité en `us1` fait traverser l'Atlantique à
la voix à chaque réplique — Twilio ouvre le flux média depuis la Virginie vers notre
serveur de Paris, et la réponse refait le chemin. Mesuré le 04/10/2026 depuis le serveur
(temps de connexion, six essais) : 83 ms d'aller-retour vers la Virginie, 19 ms vers
l'Irlande (ASSISTANTE-123).

Ce qu'une région change chez Twilio, donc ici :
- chaque région a SON jeton (Auth Token). Il signe les webhooks des appels qu'elle traite
  et authentifie son API ; le jeton d'une région est refusé par l'autre (401, vérifié) ;
- un appel et ses enregistrements vivent dans la région qui l'a traité : on les demande
  à `api.dublin.ie1.twilio.com`, pas à `api.twilio.com` ;
- les SMS restent traités en `us1`. Les deux jetons servent donc en même temps, et la
  signature (app/twilio_signature.py) accepte l'un comme l'autre.

`TWILIO_REGION` dit à quelle région NOUS nous adressons : appels sortants (app/rappel.py),
raccrochage, messages vocaux. La région où un appel ENTRANT est traité se règle chez
Twilio, numéro par numéro : `scripts/twilio_region.py` et `docs/TWILIO_SETUP.md`.
"""
import os
from typing import Optional

DEFAUT = "us1"

# Région → lieu d'accès (« edge »). Twilio veut les deux dans le nom d'hôte, sauf pour
# `us1` qui garde l'adresse historique.
_ACCES = {"us1": None, "ie1": "dublin"}


def jetons() -> dict[str, str]:
    """Les jetons posés, par région — `us1` d'abord."""
    poses = {"us1": os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
             "ie1": os.getenv("TWILIO_AUTH_TOKEN_IE1", "").strip()}
    return {region: jeton for region, jeton in poses.items() if jeton}


def demandee() -> str:
    """Ce que dit `TWILIO_REGION`, tel quel (minuscules). Vide = `us1`."""
    return os.getenv("TWILIO_REGION", "").strip().lower() or DEFAUT


def choisie() -> str:
    """La région à laquelle on s'adresse. Une région inconnue, ou dont le jeton n'est pas
    posé, vaut `us1` : un appel qui passe par les États-Unis vaut mieux qu'un appel qui
    ne part pas. `anomalie` le dit à la supervision, pour que ce repli ne dure pas."""
    region = demandee()
    return region if region == DEFAUT or region in jetons() else DEFAUT


def anomalie() -> Optional[str]:
    """Une phrase si `TWILIO_REGION` demande une région qu'on ne peut pas servir."""
    region = demandee()
    if region not in _ACCES:
        return (f"TWILIO_REGION={region} n'est pas une région connue "
                f"({', '.join(_ACCES)}) : l'application s'adresse à us1.")
    if region != choisie():
        return (f"TWILIO_REGION={region} sans le jeton de cette région "
                f"(TWILIO_AUTH_TOKEN_{region.upper()}) : l'application s'adresse à us1.")
    return None


def ordre() -> list[str]:
    """Les régions dont on a le jeton, celle qu'on a choisie d'abord. Pour ce qu'il faut
    chercher sans savoir où Twilio l'a rangé (un message vocal, les alertes)."""
    premiere = choisie()
    return sorted(jetons(), key=lambda region: region != premiere)


def acces(region: Optional[str] = None) -> Optional[str]:
    """Le lieu d'accès de la région (`dublin` pour `ie1`), ou None pour `us1`."""
    return _ACCES.get(region or choisie())


def hote(region: Optional[str] = None, produit: str = "api") -> str:
    """`https://api.twilio.com` en `us1`, `https://api.dublin.ie1.twilio.com` en `ie1`."""
    region = region or choisie()
    lieu = _ACCES.get(region)
    return f"https://{produit}.{lieu}.{region}.twilio.com" if lieu else f"https://{produit}.twilio.com"


def identifiants(region: Optional[str] = None) -> Optional[tuple[str, str]]:
    """(identifiant du compte, jeton de la région), ou None s'il en manque un."""
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    jeton = jetons().get(region or choisie(), "")
    return (sid, jeton) if sid and jeton else None
