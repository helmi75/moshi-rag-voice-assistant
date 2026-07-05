"""API du SaaS d'accueil téléphonique : webhooks Twilio multi-tenant + Claude."""
import time
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import Response

from . import db, llm, reservations, tenants

app = FastAPI(title="Voice Assistant SaaS")

db.init_db()
tenants.seed_demo_tenant()

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


def _say_and_gather(text: str, language: str) -> Response:
    return _twiml(
        f'    <Say language="{language}">{escape(text)}</Say>\n'
        f'    <Gather input="speech" language="{language}" timeout="5" speechTimeout="auto"'
        f' action="/twilio/voice" method="POST"/>\n'
        f'    <Say language="{language}">Merci pour votre appel. Au revoir.</Say>'
    )


@app.get("/health")
async def health_check():
    return {"status": "ok", "model": llm.MODEL}


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
        return _twiml(
            '    <Say language="fr-FR">Ce numéro n\'est pas encore configuré. Au revoir.</Say>\n'
            "    <Hangup/>"
        )

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
