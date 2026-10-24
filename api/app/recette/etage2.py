"""Étage 2 : de vrais appels. Twilio compose le numéro de l'établissement d'essai.

À lancer DANS le conteneur de production (`docker compose exec`, par
`scripts/recette_appels.py`) : c'est là que sont le jeton de Twilio, la base où lire le
verdict, et les répliques que `/ws/recette` jouera. Aucune clé ne quitte le serveur.

C'est le seul moyen d'éprouver sans Helmi la signature, `/twilio/voice`, l'ouverture du
flux et `/twilio/suite`. Chaque appel a deux jambes facturées par Twilio : celle qui
appelle (lue chez Twilio après l'appel) et celle qui reçoit (chiffrée par l'application).
"""
import asyncio
import contextlib
import math
import os
import time
from typing import Optional
from xml.sax.saxutils import quoteattr  # nosemgrep: use-defused-xml — échappe le TwiML qu'on écrit, ne lit aucun XML

import httpx

from .. import calls, db, essai, messages, reservations, supervision, tenants, twilio_region
from . import appelant, budget, gardes, scenarios, secours, verdicts, voix
from .tarifs import DUREE_MAX_SECONDES, borne

_DELAI = httpx.Timeout(20.0, connect=5.0)
_FINIS = ("completed", "busy", "failed", "no-answer", "canceled")
S10 = scenarios.S10
REPONDEUR = scenarios.REPONDEUR
# La porte de `secours.py` est dans le processus de production, et ce script dans le même
# conteneur : on la joint en local, sans passer par Caddy ni par Internet.
_PORTE = "http://127.0.0.1:8000/recette/secours"


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


async def _porte(action: str) -> bool:
    """Demande à la production d'`essayer` ou d'`arreter` le répondeur d'essai. Vrai si elle
    a répondu 204. Ne lève jamais : elle sert aussi dans un `finally`. Client à part : celui
    de Twilio porte les identifiants de Twilio, qui ne sortent pas vers une autre adresse."""
    nonce = gardes.nouveau_nonce()
    try:
        async with httpx.AsyncClient(timeout=_DELAI) as client:
            reponse = await client.post(_PORTE, data={
                "action": action, "nonce": nonce, "jeton": gardes.jeton("secours-" + action, nonce)})
        return reponse.status_code == 204
    except Exception:
        return False


class Essai:
    """Ce que la production a confirmé de l'arrêt du répondeur d'essai (lu par S8)."""
    demarre: bool = False
    arrete: Optional[bool] = None


@contextlib.asynccontextmanager
async def repondeur_ouvert(etat: Essai):
    """L'établissement d'essai est sur répondeur le temps du bloc. L'arrêt est demandé
    quoi qu'il arrive : un passage qui casse ne laisse jamais l'établissement d'essai sur
    répondeur (et, si le processus lui-même est tué, la mémoire des pannes l'oublie au
    bout de trois minutes)."""
    try:
        if not await _porte(secours.ESSAYER):
            raise RuntimeError("la production n'a pas mis l'établissement d'essai sur répondeur")
        etat.demarre = True
        yield etat
    finally:
        etat.arrete = await _porte(secours.ARRETER)


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


def _message_rapatrie(tenant_id: int, depuis: str) -> tuple[Optional[dict], list[dict]]:
    appel = _appel_recu(tenant_id, depuis)
    return appel, (messages.messages_d_un_appel(appel["id"]) if appel else [])


async def _repondeur_clos(tenant_id: int, depuis: str, attente: float = 90.0) -> tuple[Optional[dict], list[dict]]:
    """L'appel du répondeur, avec son message. Twilio annonce le fichier prêt quelques
    secondes APRÈS l'appel, et le rapatriement se fait en tâche de fond : on attend l'audio,
    avec une borne, et le verdict dit s'il manque."""
    fin = time.monotonic() + attente
    while True:
        appel, notes = await db.hors_boucle(_message_rapatrie, tenant_id, depuis)
        if (appel and appel.get("recording_bytes")) or time.monotonic() >= fin:
            return appel, notes
        await asyncio.sleep(2)


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
    return (gardes.la_nuit() or gardes.appel_reel_en_cours() or supervision.solde_insuffisant()
            or gardes.deja_joue(passages)
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
        a_jouer = [s for s in (REPONDEUR,) + scenarios.SCENARIOS + (S10,)
                   if not seulement or s.cle in seulement]
        essai_arrete = Essai()
        for scenario in a_jouer:
            ligne = {"etage": 2, "moteur": moteur, "scenario": scenario.cle,
                     "recette": scenario.recette, "titre": scenario.titre, "cout": 0.0}
            lignes.append(ligne)
            partis: list[str] = []
            refus_budget = budget.refus(borne(moteur, etage=2), depense)
            # Revus avant chaque scénario : un vrai appel arrivé, ou le solde Twilio relevé
            # sous le seuil pendant la recette, l'arrêtent là.
            en_cours = gardes.appel_reel_en_cours() or supervision.solde_insuffisant()
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
                # Le répondeur : l'essai tient le temps de l'appel et de l'attente de l'audio.
                ouvert = (repondeur_ouvert(essai_arrete) if scenario is REPONDEUR
                          else contextlib.nullcontext())
                notes: list[dict] = []
                async with ouvert:
                    sid = await _composer(client, tenant.phone_number, de, scenario.cle)
                    partis.append(sid)
                    etat = await _attendre_la_fin(client, sid, time.monotonic() + DUREE_MAX_SECONDES + 40)
                    if scenario is REPONDEUR:
                        appel, notes = await _repondeur_clos(tenant.id, depuis)
                    else:
                        appel = await _clos(tenant.id, depuis)
                du_client = [r for r in await db.hors_boucle(reservations.list_reservations, tenant.id)
                             if r.get("customer_phone") == de and (r.get("created_at") or "") >= depuis]
                if scenario is REPONDEUR:
                    verdict, preuve = verdicts.juger_repondeur(appel, notes, du_client)
                else:
                    verdict, preuve = verdicts.juger(scenario, appel, du_client)
                    autre = verdicts.pas_le_bon_moteur(moteur, appel, live.a_l_ecart(tenant))
                    if autre:
                        verdict, preuve = verdicts.ECHEC, autre
                if etat.get("status") != "completed":
                    verdict, preuve = verdicts.ECHEC, f"Twilio : appel {etat.get('status') or 'sans état'} ; {preuve}"
                prix, publie = await _prix(client, sid, etat)
                # Le prix que Twilio publie pour la jambe appelante ne compte pas son flux
                # audio (`/ws/recette`), facturé sur une autre ligne : on l'ajoute.
                ligne["cout"] = round(ligne["cout"] + float((appel or {}).get("estimated_cost") or 0) + prix
                                      + calls.cout_flux(float(etat.get("duration") or 0))
                                      + sum(n for _, n in rendues) * calls._COST_VOIX_PAR_CARACTERE, 6)
                if not publie:
                    preuve += " ; prix Twilio pas encore publié : jambe appelante comptée au tarif"
                ligne.update(etat=verdict, preuve=preuve, appel=appel)
                if verdict != verdicts.OK:
                    ligne["dit"] = verdicts.conversation(appel)
                # Chaque scénario repart d'un établissement vide : sinon le client suivant,
                # qui appelle du même numéro, est reconnu (« au nom de Bernard, comme la
                # dernière fois ? ») et son texte ne répond plus aux questions posées.
                ligne["purge"] = await db.hors_boucle(essai.purger)
            except Exception as exc:
                ligne.update(etat=verdicts.ECHEC,
                             preuve=f"le scénario n'a pas pu être joué : {type(exc).__name__} {exc}"[:200])
                if partis:
                    # Un appel est parti et on n'a pas pu lire ce qu'il a coûté : on compte
                    # la borne haute. Sous-compter ferait sauter le plafond en silence.
                    ligne["cout"] = max(ligne["cout"], borne(moteur, etage=2))
                    ligne["preuve"] += " ; coût non lu : borne haute comptée"
            depense += ligne["cout"]
        if REPONDEUR in a_jouer:
            # L'appel qui SUIT le répondeur : plus tard, l'essai aurait expiré de lui-même
            # (trois minutes) et un appel servi ne prouverait plus l'arrêt.
            rang = next((n for n, l in enumerate(lignes) if l["scenario"] == REPONDEUR.cle), None)
            suivant = lignes[rang + 1] if rang is not None and rang + 1 < len(lignes) else None
            lignes.append(ligne_s8(moteur, essai_arrete, suivant))
    return lignes, None


def ligne_s8(moteur: str, essai_joue: Essai, suivant: Optional[dict]) -> dict:
    """S8 : l'essai arrêté, l'appel suivant (déjà joué : rien de plus à payer) est servi par
    l'assistante. Son propre verdict peut être rouge pour une autre raison : ici on ne lit
    que ce qui dit s'il est passé en secours."""
    ligne = {"etage": 2, "moteur": moteur, "scenario": "s8", "recette": "S8", "cout": 0.0,
             "titre": "Une fois l'essai arrêté, l'assistante répond normalement"}
    appel = (suivant or {}).get("appel")
    if not essai_joue.demarre:
        etat, preuve = verdicts.NON_JOUE, "le répondeur d'essai n'a pas été mis en route"
    elif not essai_joue.arrete:
        etat, preuve = verdicts.ECHEC, "l'arrêt de l'essai n'a pas été confirmé par la production"
    elif suivant is None or suivant.get("etat") == verdicts.NON_JOUE:
        etat, preuve = verdicts.NON_JOUE, "aucun appel joué après le répondeur pour prouver S8"
    elif appel is None:
        etat, preuve = verdicts.ECHEC, f"{suivant.get('recette')} : aucun appel au journal"
    elif appel.get("secours_motif"):
        etat, preuve = verdicts.ECHEC, (f"appel {appel.get('id')} passé en secours "
                                        f"({appel['secours_motif']}) après l'arrêt")
    else:
        etat, preuve = verdicts.OK, f"essai arrêté ; appel {appel.get('id')} servi par l'assistante"
    ligne.update(etat=etat, preuve=preuve)
    return ligne
