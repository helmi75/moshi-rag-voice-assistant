"""Étage 2 : de vrais appels. Twilio compose le numéro de l'établissement d'essai.

À lancer DANS le conteneur de production (`docker compose exec`, par
`scripts/recette_appels.py`) : c'est là que sont le jeton de Twilio, la base où lire le
verdict, et les répliques que `/ws/recette` jouera. Aucune clé ne quitte le serveur.

C'est le seul moyen d'éprouver sans Helmi la signature, `/twilio/voice`, l'ouverture du
flux et `/twilio/suite`. Chaque appel a deux jambes facturées par Twilio : celle qui
appelle (lue chez Twilio après l'appel) et celle qui reçoit (chiffrée par l'application).
"""
import asyncio
import math
import os
import time
from typing import Optional
from xml.sax.saxutils import quoteattr

import httpx

from .. import calls, db, essai, reservations, tenants, twilio_region
from . import appelant, budget, gardes, scenarios, verdicts, voix
from .tarifs import DUREE_MAX_SECONDES, borne

_DELAI = httpx.Timeout(20.0, connect=5.0)
_FINIS = ("completed", "busy", "failed", "no-answer", "canceled")
# S10 : raccrocher à la première sonnerie, puis rappeler. Le second appel joue ce scénario.
S10 = scenarios.Scenario(
    "s10", "S10", "Raccrocher à la première sonnerie puis rappeler : le second appel est servi", (
        "Je voudrais réserver une table pour deux personnes lundi à dix-neuf heures.",
        "Au nom de Roux.", "Oui.", "Non merci, c'est tout. Au revoir."),
    attendu={"party_size": 2, "time": "19:00", "nom": "roux"})


def _api() -> tuple[str, tuple[str, str]]:
    acces = twilio_region.identifiants()
    if acces is None:
        raise RuntimeError("identifiants Twilio absents pour la région choisie")
    return f"{twilio_region.hote()}/2010-04-01/Accounts/{acces[0]}", acces


async def nos_numeros(client: httpx.AsyncClient) -> set[str]:
    base, _ = _api()
    reponse = await client.get(f"{base}/IncomingPhoneNumbers.json", params={"PageSize": 100})
    reponse.raise_for_status()
    return {n.get("phone_number") for n in reponse.json().get("incoming_phone_numbers", [])}


def ligne_presentee() -> str:
    """Le numéro à nous que l'appel d'essai présente : `RECETTE_LIGNE`, sinon celui du
    rappel du site."""
    return os.getenv("RECETTE_LIGNE", "").strip() or os.getenv("TWILIO_NUMBER", "").strip()


def twiml(scenario: str) -> str:
    nonce = gardes.nouveau_nonce()
    parametres = {"scenario": scenario, "nonce": nonce, "jeton": gardes.jeton(scenario, nonce)}
    return ("<Response><Connect><Stream url=" + quoteattr(appelant.url_du_flux()) + ">"
            + "".join(f"<Parameter name={quoteattr(n)} value={quoteattr(v)}/>" for n, v in parametres.items())
            + "</Stream></Connect><Hangup/></Response>")


RACCROCHE = "<Response><Hangup/></Response>"


async def _composer(client: httpx.AsyncClient, vers: str, de: str, scenario: str,
                    instructions: Optional[str] = None) -> str:
    base, _ = _api()
    reponse = await client.post(f"{base}/Calls.json", data={
        "To": vers, "From": de, "Twiml": instructions or twiml(scenario),
        "Timeout": "20", "TimeLimit": str(DUREE_MAX_SECONDES)})
    reponse.raise_for_status()
    return str(reponse.json().get("sid") or "")


async def _etat(client: httpx.AsyncClient, sid: str) -> dict:
    base, _ = _api()
    reponse = await client.get(f"{base}/Calls/{sid}.json")
    reponse.raise_for_status()
    return reponse.json()


async def _attendre_la_fin(client: httpx.AsyncClient, sid: str, jusqu_a: float) -> dict:
    etat = {}
    while time.monotonic() < jusqu_a:
        etat = await _etat(client, sid)
        if etat.get("status") in _FINIS:
            break
        await asyncio.sleep(2)
    return etat


def _appel_recu(tenant_id: int, depuis: str) -> Optional[dict]:
    """L'appel que l'application a reçu pour l'établissement d'essai depuis `depuis`."""
    with db.get_conn() as conn:
        ligne = conn.execute(
            "SELECT * FROM calls WHERE tenant_id = ? AND started_at >= ? ORDER BY id DESC LIMIT 1",
            (tenant_id, depuis)).fetchone()
    return dict(ligne) if ligne else None


async def _clos(tenant_id: int, depuis: str, attente: float = 20.0) -> Optional[dict]:
    fin, ligne = time.monotonic() + attente, None
    while time.monotonic() < fin:
        ligne = await db.hors_boucle(_appel_recu, tenant_id, depuis)
        if ligne and ligne.get("ended_at"):
            break
        await asyncio.sleep(0.5)
    return ligne


def prealables(passages: list[dict], tenant, numeros: set[str], de: str) -> Optional[str]:
    """Pourquoi aucun vrai appel ne part, ou None."""
    if not gardes.active():
        return "recette automatique non activée (RECETTE_JETON et RECETTE_ETABLISSEMENT)"
    if tenant is None:
        return "l'établissement d'essai n'existe pas"
    pas_d_essai = essai.refus(tenant.id)
    if pas_d_essai:
        return pas_d_essai
    return (gardes.la_nuit() or gardes.appel_reel_en_cours() or gardes.deja_joue(passages)
            or gardes.destinataire_refuse(tenant.phone_number, numeros, tenant.phone_number)
            or (None if de in numeros and de != tenant.phone_number
                else "la ligne présentée n'est pas un autre de nos numéros"))


async def _prix(client: httpx.AsyncClient, sid: str, etat: dict, essais: int = 6) -> tuple[float, bool]:
    """(ce que Twilio a facturé pour la jambe qui appelle, vrai si c'est le prix publié).
    Twilio publie le prix quelques minutes après l'appel : tant qu'il manque, on compte les
    minutes entamées au tarif relevé, et on le DIT."""
    for _ in range(essais):
        if etat.get("price") not in (None, ""):
            return abs(float(etat["price"])), True
        await asyncio.sleep(5)
        etat = await _etat(client, sid)
    minutes = math.ceil(int(etat.get("duration") or 0) / 60)
    return round(minutes * calls._COST_RENVOI_FIXE, 4), False


async def jouer(depense: float = 0.0, seulement: Optional[set[str]] = None) -> tuple[list[dict], Optional[str]]:
    """Les scénarios par de vrais appels. Rend (lignes, refus) : `refus` dit pourquoi rien
    n'est parti."""
    tenant = await db.hors_boucle(tenants.get_by_id, essai.etablissement_id() or 0)
    de = ligne_presentee()
    lignes: list[dict] = []
    async with httpx.AsyncClient(timeout=_DELAI, auth=_api()[1]) as client:
        numeros = await nos_numeros(client)
        refus = prealables(budget.lire(), tenant, numeros, de)
        if refus:
            return lignes, refus
        from ..voice import live

        moteur = live.moteur(tenant)
        a_jouer = [s for s in scenarios.SCENARIOS + (S10,) if not seulement or s.cle in seulement]
        for scenario in a_jouer:
            ligne = {"etage": 2, "moteur": moteur, "scenario": scenario.cle,
                     "recette": scenario.recette, "titre": scenario.titre, "cout": 0.0}
            lignes.append(ligne)
            partis: list[str] = []
            refus_budget = budget.refus(borne(moteur, etage=2), depense)
            en_cours = gardes.appel_reel_en_cours()
            if refus_budget or en_cours:
                ligne.update(etat=verdicts.NON_JOUE, preuve=refus_budget or en_cours)
                continue
            try:
                rendues = [await voix.replique(texte, scenario.langue) for texte in scenario.repliques]
                if scenario is S10:
                    # Le premier appel est raccroché dès qu'il sonne : il ne doit ni être
                    # compté comme une panne, ni empêcher le suivant d'être servi.
                    # Il ne porte aucun jeton : décroché malgré tout, il raccroche aussitôt
                    # au lieu de jouer le scénario en double.
                    premier = await _composer(client, tenant.phone_number, de, scenario.cle, RACCROCHE)
                    partis.append(premier)
                    fin = time.monotonic() + 8
                    while time.monotonic() < fin and (await _etat(client, premier)).get("status") == "queued":
                        await asyncio.sleep(0.3)
                    base, _ = _api()
                    # `canceled` n'agit que sur un appel qui sonne ; `completed` coupe aussi
                    # un appel décroché. Aucun appel d'essai ne reste ouvert derrière nous.
                    for arret in ("canceled", "completed"):
                        if (await _etat(client, premier)).get("status") in _FINIS:
                            break
                        await client.post(f"{base}/Calls/{premier}.json", data={"Status": arret})
                        await asyncio.sleep(2)
                    du_premier, _ = await _prix(client, premier, await _etat(client, premier), essais=1)
                    ligne["cout"] = du_premier
                depuis = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
                sid = await _composer(client, tenant.phone_number, de, scenario.cle)
                partis.append(sid)
                etat = await _attendre_la_fin(client, sid, time.monotonic() + DUREE_MAX_SECONDES + 40)
                appel = await _clos(tenant.id, depuis)
                du_client = [r for r in await db.hors_boucle(reservations.list_reservations, tenant.id)
                             if r.get("customer_phone") == de and (r.get("created_at") or "") >= depuis]
                verdict, preuve = verdicts.juger(scenario, appel, du_client)
                autre = verdicts.pas_le_bon_moteur(moteur, appel, live.a_l_ecart(tenant))
                if autre:
                    verdict, preuve = verdicts.ECHEC, autre
                if etat.get("status") != "completed":
                    verdict, preuve = verdicts.ECHEC, f"Twilio : appel {etat.get('status') or 'sans état'} ; {preuve}"
                prix, publie = await _prix(client, sid, etat)
                ligne["cout"] = round(ligne["cout"] + float((appel or {}).get("estimated_cost") or 0) + prix
                                      + sum(n for _, n in rendues) * calls._COST_VOIX_PAR_CARACTERE, 6)
                if not publie:
                    preuve += " ; prix Twilio pas encore publié : jambe appelante comptée au tarif"
                ligne.update(etat=verdict, preuve=preuve, appel=appel)
                if verdict != verdicts.OK:
                    ligne["dit"] = verdicts.conversation(appel)
            except Exception as exc:
                ligne.update(etat=verdicts.ECHEC,
                             preuve=f"le scénario n'a pas pu être joué : {type(exc).__name__} {exc}"[:200])
                if partis:
                    # Un appel est parti et on n'a pas pu lire ce qu'il a coûté : on compte
                    # la borne haute. Sous-compter ferait sauter le plafond en silence.
                    ligne["cout"] = max(ligne["cout"], borne(moteur, etage=2))
                    ligne["preuve"] += " ; coût non lu : borne haute comptée"
            depense += ligne["cout"]
    return lignes, None
