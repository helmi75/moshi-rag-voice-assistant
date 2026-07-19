"""API du SaaS d'accueil téléphonique : webhooks Twilio multi-tenant + Claude."""
import json
import os
import time
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from . import calls, db, llm, reservations, tenants, users

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


def _stream_twiml(request: Request, to: str, call_sid: str) -> Response:
    ws_url = _stream_ws_url(request)
    # Log explicite : si Twilio ne joint pas cette URL (mauvais tunnel ngrok, http
    # au lieu de wss...), le flux média ne se connecte jamais et l'appel raccroche.
    print(f"[stream] TwiML Media Stream → {ws_url}  (To={to}, CallSid={call_sid})")
    return _twiml(
        "    <Connect>\n"
        f'        <Stream url="{escape(ws_url)}">\n'
        f'            <Parameter name="To" value="{escape(to)}"/>\n'
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
    return {"status": "ok", "model": llm.MODEL, "voice_mode": _voice_mode()}


@app.post("/twilio/voice")
async def voice_webhook(
    request: Request,
    CallSid: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    SpeechResult: Optional[str] = Form(None),
):
    """Webhook vocal Twilio : boucle Gather/Say pilotée par le LLM du tenant."""
    tenant = tenants.get_by_phone(To)
    if tenant is None:
        not_configured = _say("Ce numéro n'est pas encore configuré. Au revoir.", "fr-FR")
        return _twiml(not_configured + "\n    <Hangup/>")

    # Mode streaming : on branche l'appel sur le pipeline Pipecat via Media Streams
    if _voice_mode() == "stream":
        return _stream_twiml(request, To or "", CallSid or "")

    # Premier tour : accueil sans appel LLM (latence nulle)
    if not SpeechResult:
        return _say_and_gather(tenant.greeting, tenant.language)

    try:
        history = _get_history(CallSid or "")
        text, messages = await llm.respond(tenant, history, SpeechResult)
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
            text, _ = await llm.respond(tenant, [], Body)
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
    to_number = (start.get("customParameters") or {}).get("To")

    tenant = tenants.get_by_phone(to_number)
    if tenant is None or not stream_sid:
        print(f"Stream refusé: tenant inconnu ou streamSid manquant (To={to_number})")
        await websocket.close(code=1008)  # policy violation
        return

    # Journal des appels (admin) : best-effort, ne doit JAMAIS faire échouer un appel.
    try:
        calls.start_call(call_sid, tenant.id)
    except Exception as exc:
        print(f"[calls] start_call KO (sans conséquence): {exc}")

    run_bot = _get_bot_runner()
    try:
        await run_bot(websocket, stream_sid, call_sid, tenant)
    except Exception as exc:
        print(f"Erreur pipeline vocal (tenant {tenant.id}, appel {call_sid}): {exc}")
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass  # déjà fermée
