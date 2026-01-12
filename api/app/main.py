from fastapi import FastAPI, Form, Request
from fastapi.responses import Response
import httpx
import os
from typing import Optional

app = FastAPI()
MOSHI_API = os.getenv("MODEL_API_URL", "http://moshi:8091")

@app.get("/health")
async def health_check():
    """Endpoint de vérification de santé"""
    return {"status": "ok", "moshi_api": MOSHI_API}

@app.post("/twilio/voice")
async def voice_webhook(
    request: Request,
    SpeechResult: Optional[str] = Form(None),
    CallSid: Optional[str] = Form(None)
):
    """
    Webhook pour les appels vocaux Twilio.
    - Premier appel: retourne un Gather pour collecter la parole
    - Appels suivants: traite SpeechResult et répond
    """
    # Si pas de SpeechResult, c'est le premier appel - demander à l'utilisateur de parler
    if not SpeechResult:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="fr-FR">Bonjour, je suis votre assistant vocal. Comment puis-je vous aider?</Say>
    <Gather input="speech" language="fr-FR" timeout="5" action="/twilio/voice" method="POST">
        <Say language="fr-FR">Je vous écoute.</Say>
    </Gather>
    <Say language="fr-FR">Je n'ai pas entendu de réponse. Au revoir.</Say>
</Response>"""
        return Response(content=twiml, media_type="text/xml")
    
    # Traiter la parole de l'utilisateur
    try:
        async with httpx.AsyncClient() as client:
            # Note: Moshi n'expose pas une API OpenAI-compatible
            # Ceci est un placeholder - à adapter selon l'API réelle de Moshi
            r = await client.post(
                f"{MOSHI_API}/v1/chat/completions",
                json={
                    "model": "moshika-pytorch-bf16",
                    "messages": [{"role": "user", "content": SpeechResult}],
                    "max_tokens": 150
                },
                timeout=60.0
            )
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"]
            else:
                text = "Désolé, je n'ai pas pu traiter votre demande."
    except Exception as e:
        print(f"Erreur appel Moshi: {e}")
        text = "Désolé, une erreur s'est produite."
    
    # Répondre et continuer la conversation
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="fr-FR">{text}</Say>
    <Gather input="speech" language="fr-FR" timeout="5" action="/twilio/voice" method="POST">
        <Say language="fr-FR">Avez-vous autre chose à demander?</Say>
    </Gather>
    <Say language="fr-FR">Merci d'avoir appelé. Au revoir.</Say>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.post("/twilio/sms")
async def sms_webhook(Body: str = Form(...), From: Optional[str] = Form(None)):
    """Webhook pour les SMS Twilio"""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{MOSHI_API}/v1/chat/completions",
                json={
                    "model": "moshika-pytorch-bf16",
                    "messages": [{"role": "user", "content": Body}],
                    "max_tokens": 150
                },
                timeout=60.0
            )
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"]
            else:
                text = "Désolé, je n'ai pas pu traiter votre message."
    except Exception as e:
        print(f"Erreur appel Moshi: {e}")
        text = "Désolé, une erreur s'est produite."
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{text}</Message>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

# Webhook générique (pour compatibilité avec l'ancien setup)
@app.post("/twilio/webhook")
async def twilio_webhook(request: Request):
    """Webhook générique qui redirige vers voice ou sms selon le type"""
    form_data = await request.form()
    
    # Déterminer le type de webhook (Voice ou SMS)
    if "CallSid" in form_data:
        return await voice_webhook(
            request,
            SpeechResult=form_data.get("SpeechResult"),
            CallSid=form_data.get("CallSid")
        )
    elif "Body" in form_data:
        return await sms_webhook(
            Body=form_data.get("Body"),
            From=form_data.get("From")
        )
    else:
        # Requête inconnue
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="text/xml"
        )
