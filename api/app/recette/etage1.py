"""Étage 1 : les scénarios joués sans Twilio, sur une copie jetable de l'application.

À lancer dans un conteneur jetable (`scripts/recette_appels.py`) : la base est un fichier
temporaire, aucun e-mail ne part, et l'application tourne dans ce même processus sur un
port local. Un faux Twilio se branche sur `/ws/voice` — le vrai chemin d'un appel, signature
exceptée — pour chaque moteur.

`preparer_l_environnement()` doit être appelée AVANT d'importer l'application : elle pose
la base jetable.
"""
import asyncio
import base64
import json
import os
import tempfile
import time
from typing import Optional

PORT = 8765
NUMERO = "+33900000001"            # celui de l'établissement jetable : rien ne le compose
MOTEURS = ("gpt_live", "classique")


def preparer_l_environnement() -> str:
    dossier = tempfile.mkdtemp(prefix="recette-")
    os.environ.update({
        "DB_PATH": f"{dossier}/essai.db", "TWILIO_SIGNATURE": "off", "SMTP_HOST": "",
        "ENREGISTREMENT_APPELS": "0", "RESUME_APPELS": "0", "RESOS_API_KEYS": "",
        "RECETTE_ETABLISSEMENT": "", "GREETING_CACHE_DIR": f"{dossier}/accueils",
        "RECETTE_REPLIQUES_DIR": os.getenv("RECETTE_REPLIQUES_DIR") or f"{dossier}/repliques",
    })
    return dossier


def _etablissement(moteur: str):
    from .. import db, tenants
    from . import scenarios

    db.init_db()
    tenant = tenants.get_by_phone(NUMERO) or tenants.create_tenant(scenarios.NOM, NUMERO)
    return tenants.update_tenant(
        tenant.id, greeting=scenarios.ACCUEIL, greeting_customized=1,
        knowledge_base=scenarios.FICHE, opening_hours=json.dumps(scenarios.HORAIRES),
        moteur_voix=moteur, booking_provider=None, notify_email=None)


async def _appel(scenario, repliques: list[bytes], call_sid: str, appelant: str) -> list[int]:
    """Joue un appel par le flux, comme Twilio. Rend les répliques dites sans attendre."""
    import websockets

    from .client import Client

    client = Client(repliques)
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/voice", max_size=None) as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({"event": "start", "streamSid": f"MZ{call_sid[2:]}", "start": {
            "streamSid": f"MZ{call_sid[2:]}", "callSid": call_sid,
            "customParameters": {"To": NUMERO, "From": appelant, "CallSid": call_sid}}}))

        async def ecouter() -> None:
            async for brut in ws:
                message = json.loads(brut)
                if message.get("event") == "media":
                    client.recu(base64.b64decode((message.get("media") or {}).get("payload") or ""))

        ecoute = asyncio.create_task(ecouter())
        prochaine = time.monotonic()
        try:
            while not client.fini and not ecoute.done():
                prochaine += 0.02
                await asyncio.sleep(max(0.0, prochaine - time.monotonic()))
                await ws.send(json.dumps({"event": "media", "media": {
                    "payload": base64.b64encode(client.trame()).decode()}}))
            if not ecoute.done():
                await ws.send(json.dumps({"event": "stop"}))
        except websockets.ConnectionClosed:
            pass                      # l'assistante a raccroché la première : c'est la fin normale
        finally:
            ecoute.cancel()
            await asyncio.gather(ecoute, return_exceptions=True)
    return client.forcees


async def _clos(call_sid: str, attente: float = 15.0) -> Optional[dict]:
    from .. import calls, db

    fin = time.monotonic() + attente
    ligne = None
    while time.monotonic() < fin:
        repere = await db.hors_boucle(calls.par_sid, call_sid)
        ligne = await db.hors_boucle(calls.get_call, repere["id"]) if repere else None
        if ligne and ligne.get("ended_at"):
            return ligne
        await asyncio.sleep(0.3)
    return ligne


async def jouer(depense: float = 0.0, moteurs=MOTEURS, seulement: Optional[set[str]] = None) -> list[dict]:
    """Tous les scénarios, pour chaque moteur. Rend une ligne par scénario joué ou refusé."""
    import uvicorn

    from .. import calls, db, reservations
    from ..main import app
    from ..voice import live
    from . import budget, scenarios, verdicts, voix
    from .tarifs import borne

    serveur = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    tache = asyncio.create_task(serveur.serve())
    while not serveur.started:
        await asyncio.sleep(0.05)
    lignes: list[dict] = []
    try:
        for moteur in moteurs:
            tenant = await db.hors_boucle(_etablissement, moteur)
            for rang, scenario in enumerate(scenarios.SCENARIOS):
                if seulement and scenario.cle not in seulement:
                    continue
                ligne = {"etage": 1, "moteur": moteur, "scenario": scenario.cle,
                         "recette": scenario.recette, "titre": scenario.titre, "cout": 0.0}
                lignes.append(ligne)
                refus = budget.refus(borne(moteur, etage=1), depense)
                if refus:
                    ligne.update(etat=verdicts.NON_JOUE, preuve=refus)
                    continue
                try:
                    rendues = [await voix.replique(texte, scenario.langue) for texte in scenario.repliques]
                    appelant = f"+3361200{MOTEURS.index(moteur)}{rang:03d}"
                    call_sid = f"{calls.PREFIXE_BANC}RECETTE{int(time.time())}{rang}"
                    debut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
                    forcees = await asyncio.wait_for(
                        _appel(scenario, [son for son, _ in rendues], call_sid, appelant), timeout=200)
                    appel = await _clos(call_sid)
                    du_client = [r for r in await db.hors_boucle(reservations.list_reservations, tenant.id)
                                 if r.get("customer_phone") == appelant and (r.get("created_at") or "") >= debut]
                    etat, preuve = verdicts.juger(scenario, appel, du_client)
                    if moteur == "classique":
                        etat, preuve = verdicts.sur_la_chaine_classique(etat, preuve)
                    autre = verdicts.pas_le_bon_moteur(moteur, appel, live.a_l_ecart(tenant))
                    if autre:
                        etat, preuve = verdicts.ECHEC, autre
                    if forcees:
                        preuve += f" ; répliques dites sans l'avoir entendue : {forcees}"
                    ligne["cout"] = round(float((appel or {}).get("estimated_cost") or 0)
                                          + sum(n for _, n in rendues) * calls._COST_VOIX_PAR_CARACTERE, 6)
                    ligne.update(etat=etat, preuve=preuve, appel=appel)
                    if etat != verdicts.OK:
                        ligne["dit"] = verdicts.conversation(appel)
                except Exception as exc:      # un scénario qui casse ne fait pas taire les autres
                    ligne.update(etat=verdicts.ECHEC, preuve=f"le scénario n'a pas pu être joué : {type(exc).__name__} {exc}"[:200])
                depense += ligne["cout"]
    finally:
        serveur.should_exit = True
        await asyncio.gather(tache, return_exceptions=True)
    return lignes
