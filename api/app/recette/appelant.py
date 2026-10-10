"""`/ws/recette` : la jambe qui APPELLE, pendant un vrai appel d'essai.

Twilio compose le numéro de l'établissement d'essai ; de l'autre côté l'assistante décroche
par le chemin de tout le monde (`/twilio/voice`, `/ws/voice`). De ce côté-ci, Twilio ouvre
ce flux, et le client d'essai (`client.py`) y parle. Rien ici ne touche au chemin d'un
appel de restaurant.

Fermé par défaut : sans `RECETTE_JETON` et `RECETTE_ETABLISSEMENT`, la route refuse avant
même d'accepter la connexion. Ouverte, elle exige la signature de Twilio, puis un jeton à
usage unique que seul le script de recette sait fabriquer.
"""
import asyncio
import base64
import json
import os

from loguru import logger
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import twilio_signature
from . import gardes, scenarios, voix
from .client import Client

_MESSAGES_AVANT_LE_DEBUT = 10


def url_du_flux() -> str:
    """L'adresse que le TwiML annonce à Twilio, et donc celle qu'il signe."""
    flux = "".join(os.getenv("PUBLIC_WS_URL", "").split())
    base = flux.rsplit("/ws/", 1)[0] if "/ws/" in flux else flux.rstrip("/")
    return f"{base}/ws/recette"


def _signee(websocket: WebSocket) -> bool:
    if twilio_signature.mode() == "off":
        return True
    return twilio_signature.region_signataire(
        url_du_flux(), {}, websocket.headers.get(twilio_signature.EN_TETE, "")) is not None


async def servir(websocket: WebSocket) -> None:
    if not gardes.active() or not _signee(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    debut = None
    try:
        for _ in range(_MESSAGES_AVANT_LE_DEBUT):
            message = json.loads(await websocket.receive_text())
            if message.get("event") == "start":
                debut = message
                break
    except Exception:      # déconnexion, trame binaire, JSON qui n'est pas un objet : même refus
        debut = None
    try:
        depart = debut.get("start") or {}
        parametres = depart.get("customParameters") or {}
        scenario = scenarios.par_cle(str(parametres.get("scenario") or ""))
        flux = debut.get("streamSid") or depart.get("streamSid")
        valable = scenario is not None and bool(flux) and gardes.jeton_valide(
            scenario.cle, str(parametres.get("nonce") or ""), str(parametres.get("jeton") or ""))
    except Exception:
        valable = False
    if not valable:
        logger.warning("[recette] flux refusé : scénario inconnu ou jeton invalide")
        await websocket.close(code=1008)
        return
    try:
        # Les répliques sont rendues AVANT l'appel par le script : ici on ne lit que le disque.
        repliques = [voix.deja_rendue(texte, scenario.langue) for texte in scenario.repliques]
    except (OSError, RuntimeError):
        logger.warning(f"[recette] répliques du scénario {scenario.cle} absentes du disque")
        await websocket.close(code=1011)
        return
    logger.info(f"[recette] le client d'essai joue {scenario.cle} (appel {depart.get('callSid')})")
    await _jouer(websocket, flux, Client(repliques))
    try:
        await websocket.close()
    except RuntimeError:
        pass


async def _jouer(websocket: WebSocket, flux: str, client: Client) -> None:
    async def ecouter() -> None:
        while True:
            try:
                message = json.loads(await websocket.receive_text())
            except (WebSocketDisconnect, RuntimeError):
                return
            except Exception:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("event") == "media":
                client.recu(base64.b64decode((message.get("media") or {}).get("payload") or ""))
            elif message.get("event") == "stop":
                return

    ecoute = asyncio.create_task(ecouter())
    prochaine = asyncio.get_running_loop().time()
    try:
        while not client.fini and not ecoute.done():
            prochaine += 0.02
            await asyncio.sleep(max(0.0, prochaine - asyncio.get_running_loop().time()))
            try:
                await websocket.send_text(json.dumps({
                    "event": "media", "streamSid": flux,
                    "media": {"payload": base64.b64encode(client.trame()).decode()}}))
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        ecoute.cancel()
        await asyncio.gather(ecoute, return_exceptions=True)
