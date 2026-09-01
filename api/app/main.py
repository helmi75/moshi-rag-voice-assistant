"""API du SaaS d'accueil téléphonique : webhooks Twilio multi-tenant + Claude."""
import json
import os
import time
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from . import calls, db, llm, reservations, rgpd, supervision, tenants, users

app = FastAPI(title="Voice Assistant SaaS")

db.init_db()
tenants.seed_demo_tenant()
users.seed_superadmin()

# --- Plateforme admin (dashboard Jinja2 + htmx) -------------------------------
# L'auth est portée par les DÉPENDANCES des routers admin (voir app/admin/) : les
# webhooks Twilio et /ws/voice ne traversent aucune logique d'auth. Le
# SessionMiddleware est global mais inerte hors admin (cookie posé seulement si la
# session est modifiée).
from pathlib import Path as _Path

from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from . import admin as admin_pkg

_session_secret = os.getenv("SESSION_SECRET", "")
if not _session_secret:
    import secrets as _secrets

    _session_secret = _secrets.token_hex(32)
    print("[admin] SESSION_SECRET absent : secret aléatoire (sessions perdues au redémarrage).")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=os.getenv("SESSION_SECURE", "").lower() in ("1", "true"),
)
app.mount("/admin/static", StaticFiles(directory=str(admin_pkg.STATIC_DIR)), name="admin_static")
app.include_router(admin_pkg.public_router)
app.include_router(admin_pkg.admin_router)


@app.on_event("startup")
async def _preload_voice_model():
    """Précharge le modèle TTS local au démarrage (mode stream + TTS_PROVIDER=pocket),
    dans un thread, pour éviter un gel de 30-60 s au tout premier appel et pour que
    les logs de démarrage confirment le bon chargement du modèle."""
    if _voice_mode() != "stream" or os.getenv("TTS_PROVIDER", "pocket").lower() != "pocket":
        return
    import asyncio

    async def _load():
        try:
            from .voice.pocket_tts import _load_model_and_state

            await asyncio.to_thread(_load_model_and_state)
            print("Modèle TTS Pocket TTS préchargé (prêt pour le premier appel).")
        except Exception as exc:
            print(f"Préchargement Pocket TTS échoué (sera retenté au 1er appel): {exc}")

    asyncio.create_task(_load())


@app.on_event("startup")
async def _prerender_greetings():
    """Phase 3 : pré-rend les accueils (voix « Développeuse ») HORS du chemin d'appel,
    pour que le tout premier appelant entende un accueil instantané. Déclenche au
    passage un cold start du GPU une seule fois, au démarrage, plutôt qu'en appel.
    Active aussi le keep-warm périodique si MOSHI_KEEPWARM_SECONDS > 0."""
    import asyncio

    from .voice import greeting as greeting_mod

    if _voice_mode() != "stream" or not greeting_mod.is_moshi_server():
        return

    async def _prerender():
        try:
            from . import tenants

            for tenant in tenants.list_all():
                await greeting_mod.ensure_greeting_wav(tenant)
        except Exception as exc:
            print(f"Pré-rendu des accueils échoué (repli TTS live au 1er appel): {exc}")

    asyncio.create_task(_prerender())
    asyncio.create_task(greeting_mod.keep_warm_loop())


# Tâches de fond permanentes, gardées en référence pour pouvoir les ARRÊTER : une
# boucle infinie qu'on abandonne empêche la boucle d'événements de se fermer, et le
# processus (ou un TestClient) attend indéfiniment. Vécu le 23/08 — le contrôle de
# mutation s'est figé une demi-heure sur un test qui sortait en erreur d'un
# `with TestClient(...)`, précisément à cause d'une tâche orpheline.
#
# Une LISTE plutôt qu'une variable par tâche : à la troisième, le motif copié-collé
# finit par oublier un arrêt quelque part.
_taches_de_fond: list = []


@app.on_event("startup")
async def _demarrer_taches_de_fond():
    """Deux boucles permanentes, chacune hors du chemin d'appel :

    - **relève des alertes Twilio** (#24) : hors de la sonde à dessein, c'est le seul
      contrôle qui exige un appel réseau et la sonde doit rester gratuite ;
    - **purge des données personnelles** (#22) : les durées de conservation ne valent
      rien tant que rien ne les APPLIQUE.
    """
    import asyncio

    from . import rgpd

    _taches_de_fond.extend([
        asyncio.create_task(supervision.boucle_twilio()),
        asyncio.create_task(rgpd.boucle()),
    ])


@app.on_event("shutdown")
async def _arreter_taches_de_fond():
    """Arrête proprement toutes les boucles. Sans ça, l'arrêt du service traîne — et un
    service qui ne sait pas s'arrêter est un service qu'on finit par tuer au signal 9,
    en pleine écriture SQLite."""
    import asyncio
    import contextlib

    taches, _taches_de_fond[:] = list(_taches_de_fond), []
    for tache in taches:
        if not tache.done():
            tache.cancel()
    for tache in taches:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tache


# Mémoire de conversation par appel (CallSid). Suffisant pour un seul process ;
# à remplacer par Redis quand l'API sera répliquée (phase 3 de la roadmap).
CONVERSATION_TTL_SECONDS = 3600
_conversations: dict[str, dict] = {}


def _get_history(call_sid: str) -> list:
    now = time.time()
    for sid in [s for s, c in _conversations.items() if now - c["ts"] > CONVERSATION_TTL_SECONDS]:
        del _conversations[sid]
    entry = _conversations.get(call_sid)
    return entry["messages"] if entry else []


def _save_history(call_sid: str, messages: list) -> None:
    _conversations[call_sid] = {"messages": messages, "ts": time.time()}


def _twiml(inner: str) -> Response:
    body = f"<Response>\n{inner}\n</Response>" if inner else "<Response></Response>"
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>\n{body}',
        media_type="text/xml",
    )


def _voice_mode() -> str:
    """"gather" (défaut, boucle Say/Gather) ou "stream" (Media Streams + Pipecat).
    Lu à chaque requête pour rester configurable sans redémarrage (et testable)."""
    return os.getenv("VOICE_MODE", "gather").strip().lower()


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
    print(f"[stream] TwiML Media Stream → {ws_url}  (To={to}, From={from_number}, CallSid={call_sid})")
    return _twiml(
        "    <Connect>\n"
        f'        <Stream url="{escape(ws_url)}">\n'
        f'            <Parameter name="To" value="{escape(to)}"/>\n'
        f'            <Parameter name="From" value="{escape(from_number)}"/>\n'
        f'            <Parameter name="CallSid" value="{escape(call_sid)}"/>\n'
        "        </Stream>\n"
        "    </Connect>"
    )


def _say_voice() -> str:
    """Voix du <Say> Twilio en mode gather. Défaut : voix neuronale Amazon Polly
    française (Léa) — naturelle, incluse dans Twilio, latence nulle. Bien meilleure
    que la voix standard robotique. Surchargeable via TWILIO_VOICE (ex. Polly.Remi-Neural,
    voix masculine). Mettre TWILIO_VOICE="" pour revenir à la voix standard."""
    return os.getenv("TWILIO_VOICE", "Polly.Lea-Neural")


def _say(text: str, language: str) -> str:
    """Balise <Say> : avec une voix Polly, la langue est portée par la voix ;
    sinon on retombe sur l'attribut language standard."""
    voice = _say_voice()
    if voice:
        return f'    <Say voice="{escape(voice)}">{escape(text)}</Say>'
    return f'    <Say language="{language}">{escape(text)}</Say>'


def _say_and_gather(text: str, language: str) -> Response:
    return _twiml(
        f"{_say(text, language)}\n"
        f'    <Gather input="speech" language="{language}" timeout="5" speechTimeout="auto"'
        f' action="/twilio/voice" method="POST"/>\n'
        f'{_say("Merci pour votre appel. Au revoir.", language)}'
    )


@app.get("/health")
async def health_check():
    """Sonde de VIE, volontairement bête et sans authentification.

    Elle répond « le processus tourne et sert du HTTP », rien de plus — c'est ce
    qu'attendent `scripts/deploy.sh` et un moniteur de disponibilité. L'état réel de
    la pile est une autre question, et elle a sa propre sonde : `/supervision`. Les
    mélanger coupleraient le déploiement à des verdicts sans rapport (une sauvegarde
    en retard n'a pas à empêcher de déployer un correctif).
    """
    return {"status": "ok", "model": llm.MODEL, "voice_mode": _voice_mode()}


@app.get("/supervision")
async def supervision_probe(request: Request):
    """Sonde d'ÉTAT, pour un surveillant extérieur à la machine.

    Pourquoi extérieur : une supervision hébergée sur le serveur qu'elle surveille ne
    signale jamais la panne qui compte le plus — celle où le serveur ne répond plus.
    Ici, l'application se contente de dire la vérité ; c'est
    `.github/workflows/supervision.yml`, qui tourne chez GitHub, qui décide d'alerter.

    Authentifiée : la réponse décrit l'infrastructure et le trafic. Comparaison à temps
    constant, comme partout ailleurs dans ce projet.

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
    fourni = (request.headers.get("x-supervision-token")
              or request.query_params.get("token") or "")
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


@app.post("/twilio/voice")
async def voice_webhook(
    request: Request,
    CallSid: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    SpeechResult: Optional[str] = Form(None),
):
    """Webhook vocal Twilio : boucle Gather/Say pilotée par le LLM du tenant."""
    tenant = tenants.get_by_phone(To)
    if tenant is None:
        not_configured = _say("Ce numéro n'est pas encore configuré. Au revoir.", "fr-FR")
        return _twiml(not_configured + "\n    <Hangup/>")

    # Mode streaming : on branche l'appel sur le pipeline Pipecat via Media Streams
    if _voice_mode() == "stream":
        return _stream_twiml(request, To or "", CallSid or "", From or "")

    # Premier tour : accueil sans appel LLM (latence nulle)
    if not SpeechResult:
        return _say_and_gather(rgpd.accueil(tenant), tenant.language)

    try:
        history = _get_history(CallSid or "")
        text, messages = await llm.respond(tenant, history, SpeechResult, From)
        if CallSid:
            _save_history(CallSid, messages)
        if not text:
            text = "Je n'ai pas bien compris, pouvez-vous répéter ?"
    except Exception as exc:
        print(f"Erreur LLM pour le tenant {tenant.id}: {exc}")
        text = "Désolé, je rencontre un problème technique. Pouvez-vous rappeler dans quelques instants ?"

    return _say_and_gather(text, tenant.language)


@app.post("/twilio/sms")
async def sms_webhook(
    Body: str = Form(...),
    From: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
):
    """Webhook SMS Twilio : réponse mono-tour via le LLM du tenant."""
    tenant = tenants.get_by_phone(To)
    if tenant is None:
        text = "Ce numéro n'est pas encore configuré."
    else:
        try:
            text, _ = await llm.respond(tenant, [], Body, From)
        except Exception as exc:
            print(f"Erreur LLM pour le tenant {tenant.id}: {exc}")
            text = "Désolé, une erreur s'est produite. Réessayez dans quelques instants."

    return _twiml(f"    <Message>{escape(text)}</Message>")


@app.post("/twilio/webhook")
async def twilio_webhook(request: Request):
    """Webhook générique : route vers voice ou sms selon la charge utile."""
    form_data = await request.form()

    if "CallSid" in form_data:
        return await voice_webhook(
            request,
            CallSid=form_data.get("CallSid"),
            To=form_data.get("To"),
            From=form_data.get("From"),
            SpeechResult=form_data.get("SpeechResult"),
        )
    if "Body" in form_data:
        return await sms_webhook(
            Body=form_data.get("Body"),
            From=form_data.get("From"),
            To=form_data.get("To"),
        )
    return _twiml("")


@app.get("/tenants/{tenant_id}/reservations")
async def tenant_reservations(tenant_id: int):
    if tenants.get_by_id(tenant_id) is None:
        raise HTTPException(status_code=404, detail="Tenant inconnu")
    return {"reservations": reservations.list_reservations(tenant_id)}


def _get_bot_runner():
    """Import paresseux du bot Pipecat (mockable en test, extras optionnels en gather)."""
    from .voice.bot import run_bot

    return run_bot


# Nombre max de messages lus avant le "start" Twilio (protocole: connected -> start)
_WS_START_MAX_MESSAGES = 10


@app.websocket("/ws/voice")
async def voice_stream(websocket: WebSocket):
    """Point d'entrée Twilio Media Streams : poignée de main puis pipeline Pipecat."""
    print("[stream] WebSocket /ws/voice : connexion entrante (Twilio a joint l'URL).")
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
    from_number = custom.get("From")

    tenant = tenants.get_by_phone(to_number)
    if tenant is None or not stream_sid:
        print(f"Stream refusé: tenant inconnu ou streamSid manquant (To={to_number})")
        await websocket.close(code=1008)  # policy violation
        return

    # Journal des appels (admin) : best-effort, ne doit JAMAIS faire échouer un appel.
    # L'identifiant rendu nomme les fichiers d'enregistrement (#88) ; à None, l'appel a
    # lieu normalement mais n'est pas enregistré — on n'invente pas de clé de fichier.
    call_id = None
    try:
        call_id = calls.start_call(call_sid, tenant.id, from_number)
    except Exception as exc:
        print(f"[calls] start_call KO (sans conséquence): {exc}")

    run_bot = _get_bot_runner()
    try:
        await run_bot(websocket, stream_sid, call_sid, tenant,
                      caller_number=from_number, call_id=call_id)
    except Exception as exc:
        print(f"Erreur pipeline vocal (tenant {tenant.id}, appel {call_sid}): {exc}")
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass  # déjà fermée
