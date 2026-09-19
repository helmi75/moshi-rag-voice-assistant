"""API du SaaS d'accueil téléphonique : webhooks Twilio multi-tenant, flux média, admin.

Un seul chemin d'appel : Twilio Media Streams → Pipecat (Deepgram, LLM via OpenRouter,
voix Moshi servie par moshi-server). La boucle Gather/Say et les moteurs locaux (Pocket
TTS, Kyutai en PyTorch) ont été retirés le 18/09/2026 : deux chemins, c'était deux
produits à tester, et le second n'était plus ni journalisé ni compté au forfait. Le code
reste lisible au tag `archive/moteurs-locaux`.
"""
import json
import os
from contextlib import asynccontextmanager
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from loguru import logger

from . import calls, db, llm, supervision, taches, tenants, twilio_signature, users


@asynccontextmanager
async def _cycle_de_vie(_app: FastAPI):
    """Démarrage puis arrêt, dans cet ordre et à un seul endroit.

    Remplace les quatre `@app.on_event` (dépréciés par FastAPI) : l'ordre y dépendait de
    l'ordre de déclaration dans le fichier, et rien ne garantissait que l'arrêt suive
    un démarrage qui aurait échoué à mi-chemin. Ici `finally` arrête les tâches de fond
    dans tous les cas."""
    await _partager_le_modele_de_fin_de_tour()
    await _prerender_greetings()
    await _demarrer_taches_de_fond()
    try:
        yield
    finally:
        await _arreter_taches_de_fond()


app = FastAPI(title="Voice Assistant SaaS", lifespan=_cycle_de_vie)

db.init_db()
tenants.seed_demo_tenant()
users.seed_superadmin()

# --- Plateforme admin (dashboard Jinja2 + htmx) -------------------------------
# L'auth est portée par les DÉPENDANCES des routers admin (voir app/admin/) : les
# webhooks Twilio et /ws/voice ne traversent aucune logique d'auth. Le
# SessionMiddleware est global mais inerte hors admin (cookie posé seulement si la
# session est modifiée).
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from . import admin as admin_pkg

_session_secret = os.getenv("SESSION_SECRET", "")
if not _session_secret:
    import secrets as _secrets

    _session_secret = _secrets.token_hex(32)
    logger.warning("[admin] SESSION_SECRET absent : secret aléatoire (sessions perdues au "
                   "redémarrage).")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=os.getenv("SESSION_SECURE", "").lower() in ("1", "true"),
)
app.mount("/admin/static", StaticFiles(directory=str(admin_pkg.STATIC_DIR)), name="admin_static")
app.include_router(admin_pkg.public_router)
app.include_router(admin_pkg.admin_router)


async def _partager_le_modele_de_fin_de_tour():
    """Charge le modèle smart-turn une seule fois pour tout le processus (#40).

    Sans ceci, il est relu du disque à CHAQUE appel, dans la boucle d'événements : au
    banc d'essai, huit appels simultanés produisaient huit chargements sérialisés et le
    dernier appelant attendait 6,2 s avant d'entendre l'accueil — un fichier WAV en
    cache, qui ne dépend pourtant d'aucun modèle.

    Posé au démarrage et non au premier appel : le détournement doit être en place avant
    que Pipecat ne construise son premier analyseur."""
    from .voice import modeles

    if modeles.partager_le_modele_de_fin_de_tour():
        logger.info("[voix] modèle de fin de tour : partagé entre les appels")


async def _prerender_greetings():
    """Phase 3 : pré-rend les accueils (voix « Développeuse ») HORS du chemin d'appel,
    pour que le tout premier appelant entende un accueil instantané. Déclenche au
    passage un cold start du GPU une seule fois, au démarrage, plutôt qu'en appel.
    Active aussi le keep-warm périodique si MOSHI_KEEPWARM_SECONDS > 0."""
    from .voice import greeting as greeting_mod

    if not greeting_mod.is_moshi_server():
        return

    async def _prerender():
        try:
            from . import tenants

            for tenant in tenants.list_all():
                await greeting_mod.ensure_greeting_wav(tenant)
        except Exception as exc:
            logger.warning(f"Pré-rendu des accueils échoué (repli TTS live au 1er appel): {exc}")

    taches.lancer(_prerender(), nom="pré-rendu des accueils")
    taches.lancer(greeting_mod.keep_warm_loop(), nom="keep-warm moshi-server")


async def _demarrer_taches_de_fond():
    """Deux boucles permanentes, chacune hors du chemin d'appel :

    - **relève des alertes Twilio** (#24) : hors de la sonde à dessein, c'est le seul
      contrôle qui exige un appel réseau et la sonde doit rester gratuite ;
    - **purge des données personnelles** (#22) : les durées de conservation ne valent
      rien tant que rien ne les APPLIQUE.

    Retenues par le registre `taches`, comme tout ce qui tourne en fond : une boucle
    infinie qu'on abandonne empêche la boucle d'événements de se fermer, et le processus
    — ou un TestClient — attend indéfiniment. Vécu le 23/08.
    """
    from . import rgpd

    taches.lancer(supervision.boucle_twilio(), nom="relève des alertes Twilio")
    taches.lancer(rgpd.boucle(), nom="purge des données personnelles")


async def _arreter_taches_de_fond():
    """Arrête proprement toutes les tâches de fond. Sans ça, l'arrêt du service traîne —
    et un service qui ne sait pas s'arrêter est un service qu'on finit par tuer au
    signal 9, en pleine écriture SQLite."""
    await taches.arreter_tout()


def _twiml(inner: str) -> Response:
    body = f"<Response>\n{inner}\n</Response>" if inner else "<Response></Response>"
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>\n{body}',
        media_type="text/xml",
    )


def _stream_ws_url(request: Request) -> str:
    """URL WebSocket annoncée à Twilio. PUBLIC_WS_URL prime (derrière un proxy,
    l'hôte vu par l'app n'est pas forcément le domaine public).

    On retire TOUTE espace de l'URL : une URL n'en contient jamais, et une seule
    espace parasite (souvent une espace insécable \\xa0 issue d'un copier-coller
    depuis un navigateur ou un chat) suffit à empêcher Twilio de joindre le flux
    média — l'appel raccroche alors sans un mot."""
    explicit = os.getenv("PUBLIC_WS_URL")
    if explicit:
        # str.split() sans argument découpe sur toute espace Unicode, \xa0 compris ;
        # "".join(...) les supprime toutes (début, fin et milieu).
        return "".join(explicit.split())
    return f"wss://{request.url.netloc}/ws/voice"


def _stream_twiml(request: Request, to: str, call_sid: str, from_number: str = "") -> Response:
    ws_url = _stream_ws_url(request)
    # Log explicite : si Twilio ne joint pas cette URL (mauvais tunnel ngrok, http
    # au lieu de wss...), le flux média ne se connecte jamais et l'appel raccroche.
    # Le numéro de l'appelant est tronqué : les journaux Docker n'ont pas de durée de
    # conservation, contrairement à la base (RETENTION_NUMERO_JOURS). Deux chiffres
    # suffisent à reconnaître son propre appel de test.
    appelant = f"…{from_number[-2:]}" if from_number else "masqué"
    logger.info(f"[stream] TwiML Media Stream → {ws_url}  (To={to}, From={appelant}, "
                f"CallSid={call_sid})")
    return _twiml(
        "    <Connect>\n"
        f'        <Stream url="{escape(ws_url)}">\n'
        f'            <Parameter name="To" value="{escape(to)}"/>\n'
        f'            <Parameter name="From" value="{escape(from_number)}"/>\n'
        f'            <Parameter name="CallSid" value="{escape(call_sid)}"/>\n'
        "        </Stream>\n"
        "    </Connect>"
    )


def _raccrocher(texte: str) -> Response:
    """TwiML qui dit une phrase puis raccroche : le seul cas où l'application parle
    elle-même, sans le pipeline — un numéro que personne n'a configuré."""
    return _twiml(f'    <Say language="fr-FR">{escape(texte)}</Say>\n    <Hangup/>')


@app.get("/health")
async def health_check():
    """Sonde de VIE, volontairement bête et sans authentification.

    Elle répond « le processus tourne et sert du HTTP », rien de plus — c'est ce
    qu'attendent `scripts/deploy.sh` et un moniteur de disponibilité. L'état réel de
    la pile est une autre question, et elle a sa propre sonde : `/supervision`. Les
    mélanger coupleraient le déploiement à des verdicts sans rapport (une sauvegarde
    en retard n'a pas à empêcher de déployer un correctif).
    """
    return {"status": "ok", "model": llm.MODEL}


@app.get("/supervision")
async def supervision_probe(request: Request):
    """Sonde d'ÉTAT, pour un surveillant extérieur à la machine.

    Pourquoi extérieur : une supervision hébergée sur le serveur qu'elle surveille ne
    signale jamais la panne qui compte le plus — celle où le serveur ne répond plus.
    Ici, l'application se contente de dire la vérité ; c'est
    `.github/workflows/supervision.yml`, qui tourne chez GitHub, qui décide d'alerter.

    Authentifiée : la réponse décrit l'infrastructure et le trafic. Comparaison à temps
    constant, comme partout ailleurs dans ce projet. Jeton en EN-TÊTE uniquement : dans
    l'URL (`?token=`), il finirait dans les journaux de Caddy et l'historique des shells.

    Codes : **200** si tout va bien ou si la pile est dégradée mais sert les appels,
    **503** si un appelant qui téléphone maintenant n'est pas correctement servi. C'est
    ce code, et lui seul, qui déclenche une alerte.
    """
    import asyncio
    import hmac

    attendu = os.getenv("SUPERVISION_TOKEN", "").strip()
    if not attendu:
        # Pas de jeton configuré = pas de sonde ici. Répondre autre chose laisserait
        # croire qu'une supervision existe alors que rien ne la protège.
        raise HTTPException(status_code=404, detail="Not Found")
    fourni = request.headers.get("x-supervision-token") or ""
    if not hmac.compare_digest(attendu, fourni):
        raise HTTPException(status_code=401, detail="Jeton de supervision invalide")

    # Lectures SQLite synchrones : hors event loop, comme partout ailleurs, pour
    # qu'une sonde interrogée pendant un appel ne fasse pas bégayer la voix.
    etat = await asyncio.to_thread(supervision.etat)
    code = 503 if etat["niveau"] == supervision.PANNE else 200
    return Response(
        content=json.dumps(etat, ensure_ascii=False, indent=1),
        media_type="application/json",
        status_code=code,
    )


# Les trois webhooks exigent la signature de Twilio (twilio_signature.exiger) : le numéro
# d'un restaurant est public, et sans elle n'importe qui ferait parler l'assistante — et
# payer le LLM. L'appel interne de twilio_webhook vers voice_webhook ne repasse pas par
# la dépendance : une seule vérification par requête.
@app.post("/twilio/voice", dependencies=[Depends(twilio_signature.exiger)])
async def voice_webhook(
    request: Request,
    CallSid: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
):
    """Webhook vocal Twilio : branche l'appel sur le pipeline Pipecat via Media Streams.

    Le numéro appelé (`To`) désigne l'établissement ; un numéro que personne n'a
    configuré est raccroché poliment plutôt que routé au hasard."""
    tenant = await db.hors_boucle(tenants.get_by_phone, To)
    if tenant is None:
        return _raccrocher("Ce numéro n'est pas encore configuré. Au revoir.")
    return _stream_twiml(request, To or "", CallSid or "", From or "")


@app.post("/twilio/sms", dependencies=[Depends(twilio_signature.exiger)])
async def sms_webhook(
    Body: str = Form(...),
    From: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
):
    """Webhook SMS Twilio : réponse mono-tour via le LLM du tenant."""
    tenant = await db.hors_boucle(tenants.get_by_phone, To)
    if tenant is None:
        text = "Ce numéro n'est pas encore configuré."
    else:
        try:
            text, _ = await llm.respond(tenant, [], Body, calls.numero_appelant(From))
        except Exception as exc:
            logger.error(f"Erreur LLM pour le tenant {tenant.id}: {exc}")
            text = "Désolé, une erreur s'est produite. Réessayez dans quelques instants."

    return _twiml(f"    <Message>{escape(text)}</Message>")


@app.post("/twilio/webhook", dependencies=[Depends(twilio_signature.exiger)])
async def twilio_webhook(request: Request):
    """Webhook générique : route vers voice ou sms selon la charge utile."""
    form_data = await request.form()

    if "CallSid" in form_data:
        return await voice_webhook(
            request,
            CallSid=form_data.get("CallSid"),
            To=form_data.get("To"),
            From=form_data.get("From"),
        )
    if "Body" in form_data:
        return await sms_webhook(
            Body=form_data.get("Body"),
            From=form_data.get("From"),
            To=form_data.get("To"),
        )
    return _twiml("")


def _get_bot_runner():
    """Import paresseux du bot Pipecat (mockable en test)."""
    from .voice.bot import run_bot

    return run_bot


# Nombre max de messages lus avant le "start" Twilio (protocole: connected -> start)
_WS_START_MAX_MESSAGES = 10


@app.websocket("/ws/voice")
async def voice_stream(websocket: WebSocket):
    """Point d'entrée Twilio Media Streams : poignée de main puis pipeline Pipecat."""
    logger.info("[stream] WebSocket /ws/voice : connexion entrante (Twilio a joint l'URL).")
    # La signature de la poignée de main est vérifiée AVANT d'accepter : refuser ici
    # coûte une réponse HTTP ; accepter puis fermer aurait déjà ouvert un flux.
    if not twilio_signature.verifier_ws(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    start_data = None
    try:
        for _ in range(_WS_START_MAX_MESSAGES):
            message = json.loads(await websocket.receive_text())
            if message.get("event") == "start":
                start_data = message
                break
    except (WebSocketDisconnect, json.JSONDecodeError, TypeError):
        pass

    if not start_data:
        await websocket.close(code=1002)  # protocole non respecté
        return

    start = start_data.get("start") or {}
    stream_sid = start_data.get("streamSid") or start.get("streamSid")
    call_sid = start.get("callSid")
    custom = start.get("customParameters") or {}
    to_number = custom.get("To")
    from_number = calls.numero_appelant(custom.get("From"))

    tenant = await db.hors_boucle(tenants.get_by_phone, to_number)
    if tenant is None or not stream_sid:
        logger.warning(f"Stream refusé: tenant inconnu ou streamSid manquant (To={to_number})")
        await websocket.close(code=1008)  # policy violation
        return

    # Journal des appels (admin) : best-effort, ne doit JAMAIS faire échouer un appel.
    # L'identifiant rendu nomme les fichiers d'enregistrement (#88) ; à None, l'appel a
    # lieu normalement mais n'est pas enregistré — on n'invente pas de clé de fichier.
    call_id = None
    try:
        call_id = await db.hors_boucle(calls.start_call, call_sid, tenant.id, from_number)
    except Exception as exc:
        logger.warning(f"[calls] start_call KO (sans conséquence): {exc}")

    run_bot = _get_bot_runner()
    try:
        await run_bot(websocket, stream_sid, call_sid, tenant,
                      caller_number=from_number, call_id=call_id)
    except Exception as exc:
        # Avec la pile d'appels : c'est l'erreur qu'on aura à diagnostiquer, et le
        # message seul (« 'NoneType' object… ») ne dit jamais où.
        logger.exception(f"Erreur pipeline vocal (tenant {tenant.id}, appel {call_sid}): {exc}")
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass  # déjà fermée
