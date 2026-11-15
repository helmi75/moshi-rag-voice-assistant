"""La fiche Google Maps d'un restaurant, pour la démonstration personnalisée (ASSISTANTE-189).

Le lecteur de site seul (`demos.lire_le_site`) n'a trouvé que l'adresse et le téléphone sur un
vrai restaurant : la plupart des sites n'ont ni carte lisible ni horaires. Helmi a choisi le
15/11/2026, en connaissance de cause, `gosom/google-maps-scraper` v1.18.1 (MIT) : son usage est
contraire aux conditions de Google et il casse quand Google change sa page. C'est pourquoi il ne
tourne qu'en PRÉPRODUCTION (`docker-compose.preprod.yml`, jamais `docker-compose.yml`) et que tout
est inactif tant que `GOSOM_URL` est vide.

Ce module parle à l'API REST du mode `-web` de gosom (réseau Docker privé, sans port publié) :
crée une recherche, attend, télécharge le CSV, puis EFFACE la recherche, toujours. gosom ne
garde rien sur disque (`tmpfs`).

Le CSV de gosom porte des avis, le nom et la photo de leurs auteurs, le propriétaire, des
e-mails : des données de personnes dont Helmane n'a que faire. Elles ne sont donc jamais
LUES — `_lignes` ne retient que les colonnes de `COLONNES` — ni gardées, ni journalisées.
"""
import asyncio
import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from . import disponibilite, horloge

DELAI_TOTAL_SECONDES = 120.0   # toute la recherche, de la création au téléchargement
PAS_SECONDES = 3.0             # entre deux relevés de l'état de la recherche
DELAI_REQUETE_SECONDES = 10.0
TAILLE_MAX = 1024 * 1024       # le CSV d'une recherche d'un seul établissement
FICHES_MAX = 5                 # au-delà, la recherche est trop vague pour être proposée
_FIN_DE_NUIT_MAX = disponibilite._FIN_DE_NUIT_MAX

# La liste blanche : les seules colonnes du CSV que ce module lit. Tout le reste (user_reviews,
# user_reviews_extended, owner, emails, images, popular_times…) n'est jamais copié.
COLONNES = ("title", "category", "address", "complete_address", "phone", "website",
            "open_hours", "descriptions", "about", "link")

# Un transport de remplacement, pour les tests : la suite ne touche jamais le réseau.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


class MapsIndisponible(Exception):
    """La fiche n'a pas pu être cherchée ; la phrase dit pourquoi (affichée à Helmi)."""


def adresse_du_service() -> str:
    return os.getenv("GOSOM_URL", "").strip().rstrip("/")


def actif() -> bool:
    """Lue à chaque appel (un test, un redémarrage la change). Vide : la fonction n'existe pas."""
    return bool(adresse_du_service())


# --- Lecture du CSV -------------------------------------------------------------------

def _lignes(contenu: str) -> list[dict]:
    """Les lignes du CSV, réduites aux colonnes de la liste blanche."""
    lecteur = csv.DictReader(io.StringIO(contenu))
    return [{c: (ligne.get(c) or "").strip() for c in COLONNES} for ligne in lecteur]


_PLAGE = re.compile(r"(\d{1,2})\s*[:hH]\s*(\d{2})?\s*[–—-]\s*(\d{1,2})\s*[:hH]\s*(\d{2})?")


def _hhmm(h: str, m: Optional[str]) -> Optional[str]:
    heures, minutes = int(h), int(m or 0)
    if heures > 24 or minutes > 59 or (heures == 24 and minutes):
        return None
    return f"{heures % 24:02d}:{minutes:02d}"


def horaires_de(brut: str) -> str:
    """`open_hours` de gosom → JSON de `disponibilite.py`, ou « » quand on ne sait pas dire.

    Vide plutôt que faux : des horaires inventés feraient REFUSER de vraies réservations
    (`disponibilite`), alors que des horaires absents n'en refusent aucune. Sont donc vides :
    un jour manquant, « Ouvert 24h/24 » (une plage 00:00–24:00 n'existe pas dans le format),
    une plage illisible, une fin avant le début au-delà de 06:00 (une faute, pas un service de
    nuit — même seuil que `disponibilite`), et tout ce que `disponibilite.charger` refuse."""
    try:
        jours = json.loads(brut)
    except ValueError:
        return ""
    if not isinstance(jours, dict):
        return ""
    semaine = {}
    for jour in horloge.JOURS:
        valeur = jours.get(jour)
        if not isinstance(valeur, list):
            return ""
        plages = []
        for texte in valeur:
            texte = str(texte)
            if "ferm" in texte.casefold():
                continue
            m = _PLAGE.fullmatch(texte.strip())
            if not m:
                return ""   # « Ouvert 24h/24 », « 7 AM–2 AM »…
            debut, fin = _hhmm(m[1], m[2]), _hhmm(m[3], m[4])
            if debut is None or fin is None:
                return ""
            if debut == fin == "00:00":
                return ""   # « 00:00–24:00 » : ouvert toute la journée, comme « Ouvert 24h/24 »
            if fin < debut and disponibilite._heure(fin) > _FIN_DE_NUIT_MAX:
                return ""
            plages.append([debut, fin])
        semaine[jour] = plages
    texte_json = json.dumps({"semaine": semaine}, ensure_ascii=False)
    charges = disponibilite.charger(texte_json)
    if charges is None or disponibilite.toujours_ferme(charges):
        return ""
    return texte_json


def _accessibilite(brut: str) -> str:
    """Le texte des options d'accessibilité que la fiche annonce (`enabled: true`)."""
    try:
        groupes = json.loads(brut)
    except ValueError:
        return ""
    if not isinstance(groupes, list):
        return ""
    options = []
    for groupe in groupes:
        if not isinstance(groupe, dict) or "accessib" not in str(groupe.get("id", "")).casefold():
            continue
        for option in groupe.get("options") or []:
            if isinstance(option, dict) and option.get("enabled") is True and option.get("name"):
                options.append(" ".join(str(option["name"]).split()))
    return ", ".join(options)


def _adresse(ligne: dict) -> str:
    if ligne["address"]:
        return ligne["address"]
    try:
        a = json.loads(ligne["complete_address"])
    except ValueError:
        return ""
    if not isinstance(a, dict):
        return ""
    ville = " ".join(str(a.get(c) or "") for c in ("postal_code", "city")).strip()
    return ", ".join(x for x in (str(a.get("street") or "").strip(), ville) if x)


def fiches_de(contenu_csv: str) -> list[dict]:
    """Le CSV → au plus `FICHES_MAX` fiches `{titre, categorie, adresse, telephone, site,
    horaires, horaires_brut, description, acces, lien}`, sans rien d'autre."""
    fiches = []
    for ligne in _lignes(contenu_csv):
        if not ligne["title"]:
            continue
        fiches.append({
            "titre": ligne["title"], "categorie": ligne["category"], "adresse": _adresse(ligne),
            "telephone": ligne["phone"], "site": ligne["website"],
            "horaires": horaires_de(ligne["open_hours"]) if ligne["open_hours"] else "",
            "horaires_brut": ligne["open_hours"],
            "description": ligne["descriptions"], "acces": _accessibilite(ligne["about"]),
            "lien": ligne["link"],
        })
    return fiches[:FICHES_MAX]


# --- Le service gosom -----------------------------------------------------------------

async def _telecharger(client: httpx.AsyncClient, identifiant: str) -> str:
    recu = bytearray()
    async with client.stream("GET", f"/api/v1/jobs/{identifiant}/download") as reponse:
        if reponse.status_code != 200:
            raise MapsIndisponible("Google Maps n'a pas rendu la fiche (téléchargement refusé).")
        async for bloc in reponse.aiter_bytes():
            recu += bloc
            if len(recu) > TAILLE_MAX:
                raise MapsIndisponible("La réponse de Google Maps est trop volumineuse : elle n'est pas lue.")
    return bytes(recu).decode("utf-8", errors="replace")


async def _recherche(client: httpx.AsyncClient, nom: str, ville: str, suivi: dict) -> str:
    reponse = await client.post("/api/v1/jobs", json={
        "name": "helmane-demo", "keywords": [f"{nom} {ville}"], "lang": "fr", "depth": 1,
        "max_time": int(DELAI_TOTAL_SECONDES) - 20})
    if reponse.status_code >= 300 or not isinstance(reponse.json().get("id"), str):
        raise MapsIndisponible("Le service de recherche Google Maps a refusé la recherche.")
    suivi["id"] = reponse.json()["id"]
    while True:
        etat = (await client.get(f"/api/v1/jobs/{suivi['id']}")).json().get("Status")
        if etat == "ok":
            return await _telecharger(client, suivi["id"])
        if etat == "failed":
            raise MapsIndisponible("La recherche Google Maps a échoué.")
        await asyncio.sleep(PAS_SECONDES)


def _age_secondes(date: str) -> Optional[float]:
    """L'âge d'une recherche d'après sa date de création (RFC 3339, parfois à la nanoseconde)."""
    try:
        d = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", str(date)).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).total_seconds()


async def _menage(client: httpx.AsyncClient) -> None:
    """Efface les recherches orphelines : gosom n'arrête pas une recherche en cours sur DELETE,
    et une réponse de création perdue, ou une recherche finie après notre abandon, laisse un
    restaurant démarché dans gosom. Est orpheline toute recherche plus vieille que notre délai
    total (aucune des nôtres ne peut encore être attendue). Ne lit jamais un CSV et ne fait
    jamais échouer la recherche : les erreurs sont avalées, le journal ne dit rien du contenu."""
    try:
        reponse = await asyncio.wait_for(client.get("/api/v1/jobs"), DELAI_REQUETE_SECONDES)
        recherches = reponse.json() if reponse.status_code == 200 else []
        for r in recherches if isinstance(recherches, list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("ID"), str):
                continue
            age = _age_secondes(r.get("Date"))
            if age is not None and age > DELAI_TOTAL_SECONDES:
                await _effacer(client, r["ID"])
    except Exception as exc:   # noqa: BLE001 — le ménage ne doit jamais faire échouer la recherche
        logger.warning(f"[fiche_maps] ménage impossible ({type(exc).__name__})")


async def chercher(nom: str, ville: str) -> list[dict]:
    """Les fiches Google Maps du restaurant (au plus `FICHES_MAX`), ou `MapsIndisponible`.
    Liste vide : la recherche a abouti sans rien trouver. Inactif (`GOSOM_URL` vide) : vide."""
    if not actif():
        return []
    suivi: dict = {}
    try:
        async with httpx.AsyncClient(base_url=adresse_du_service(), timeout=DELAI_REQUETE_SECONDES,
                                     transport=_TRANSPORT) as client:
            await _menage(client)
            try:
                contenu = await asyncio.wait_for(_recherche(client, nom, ville, suivi),
                                                 DELAI_TOTAL_SECONDES)
            finally:
                # Toujours, même après un échec ou un délai : la recherche d'un restaurant
                # démarché ne reste pas dans gosom.
                if suivi.get("id"):
                    await _effacer(client, suivi["id"])
        return fiches_de(contenu)
    except asyncio.TimeoutError:
        raise MapsIndisponible("Google Maps n'a pas répondu à temps.") from None
    except MapsIndisponible:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"[fiche_maps] service injoignable ({type(exc).__name__})")
        raise MapsIndisponible("Le service de recherche Google Maps est injoignable.") from None


async def _effacer(client: httpx.AsyncClient, identifiant: str) -> None:
    try:
        await asyncio.wait_for(client.delete(f"/api/v1/jobs/{identifiant}"), DELAI_REQUETE_SECONDES)
    except Exception as exc:   # noqa: BLE001 — l'effacement ne doit jamais masquer la cause
        logger.warning(f"[fiche_maps] recherche non effacée ({type(exc).__name__})")
