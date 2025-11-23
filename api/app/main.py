from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import Response
from .reservations import create_reservation, list_reservations
from .rag import retrieve_context
import os, httpx
import json

app = FastAPI(title="Fouquets Voice Assistant - API")
MODEL_API = os.getenv("MODEL_API_URL", "http://moshi:8091")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/health/moshi")
async def health_moshi():
    """Vérifie que le serveur Moshi est accessible"""
    try:
        resp = httpx.get(f"{MODEL_API}", timeout=5.0)
        return {"status": "ok", "moshi_status": resp.status_code}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/twilio/webhook")
async def twilio_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Webhook Twilio pour recevoir les appels et SMS
    Retourne du TwiML (Twilio Markup Language) pour répondre
    """
    form = await request.form()
    
    # Détecter le type de requête (appel ou SMS)
    call_sid = form.get('CallSid')
    message_sid = form.get('MessageSid')
    
    # Pour les SMS
    if message_sid:
        user_text = form.get('Body', '')
        from_number = form.get('From', '')
        
        if not user_text:
            # Réponse par défaut si pas de texte
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>Bonjour ! Je suis Fabieng, l'assistant du Fouquet's. Comment puis-je vous aider ?</Message>
</Response>'''
            return Response(content=twiml, media_type="application/xml")
        
        # Traiter le message avec Moshi
        contexts = retrieve_context(user_text, top_k=3)
        system = ("Tu es Fabieng, assistant téléphonique du Fouquet's. Réponds en français soigné, "
                  "souriant, efficace. Utilise les informations du restaurant fournies.")
        prompt = system + "\n\nContexte récupéré :\n" + "\n---\n".join(contexts) + "\n\nQuestion : " + user_text

        text = "Désolé, je n'ai pas pu traiter votre demande."
        
        try:
            resp = httpx.post(
                f"{MODEL_API}/api/predict",
                json={
                    "data": [prompt, system],
                    "fn_index": 0
                },
                timeout=120.0
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data and len(data["data"]) > 0:
                    result = data["data"][0]
                    if isinstance(result, list) and len(result) > 0:
                        text = result[0] if isinstance(result[0], str) else str(result[0])
                    elif isinstance(result, str):
                        text = result
                    else:
                        text = str(result)
        except Exception as e:
            text = f"Désolé, une erreur s'est produite: {str(e)}"

        # Enregistrer la réservation si mentionnée
        if "réserv" in user_text.lower():
            create_reservation({"raw": user_text, "contact": from_number})

        # Retourner la réponse en TwiML pour SMS
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{text}</Message>
</Response>'''
        return Response(content=twiml, media_type="application/xml")
    
    # Pour les appels vocaux
    elif call_sid:
        # Pour les appels, on utilise <Say> pour la synthèse vocale
        # Note: Pour une vraie intégration vocale avec Moshi, il faudrait utiliser Twilio Media Streams
        call_status = form.get('CallStatus')
        
        if call_status == 'ringing' or not form.get('SpeechResult'):
            # Premier appel ou pas encore de transcription
            twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="fr-FR">Bonjour, je suis Fabieng, l'assistant du Fouquet's. Comment puis-je vous aider aujourd'hui ?</Say>
    <Gather input="speech" language="fr-FR" timeout="3" speechTimeout="auto">
        <Say language="fr-FR">Je vous écoute.</Say>
    </Gather>
    <Say language="fr-FR">Désolé, je n'ai pas entendu. Au revoir.</Say>
</Response>'''
        else:
            # Traiter la transcription vocale
            user_text = form.get('SpeechResult', '')
            from_number = form.get('From', '')
            
            contexts = retrieve_context(user_text, top_k=3)
            system = ("Tu es Fabieng, assistant téléphonique du Fouquet's. Réponds en français soigné, "
                      "souriant, efficace. Utilise les informations du restaurant fournies.")
            prompt = system + "\n\nContexte récupéré :\n" + "\n---\n".join(contexts) + "\n\nQuestion : " + user_text

            text = "Désolé, je n'ai pas pu traiter votre demande."
            
            try:
                resp = httpx.post(
                    f"{MODEL_API}/api/predict",
                    json={
                        "data": [prompt, system],
                        "fn_index": 0
                    },
                    timeout=120.0
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    if "data" in data and len(data["data"]) > 0:
                        result = data["data"][0]
                        if isinstance(result, list) and len(result) > 0:
                            text = result[0] if isinstance(result[0], str) else str(result[0])
                        elif isinstance(result, str):
                            text = result
                        else:
                            text = str(result)
            except Exception as e:
                text = f"Désolé, une erreur s'est produite: {str(e)}"

            if "réserv" in user_text.lower():
                create_reservation({"raw": user_text, "contact": from_number})

            # Répondre avec synthèse vocale
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="fr-FR">{text}</Say>
    <Gather input="speech" language="fr-FR" timeout="3" speechTimeout="auto">
        <Say language="fr-FR">Autre chose ?</Say>
    </Gather>
    <Say language="fr-FR">Au revoir et à bientôt au Fouquet's.</Say>
    <Hangup/>
</Response>'''
        
        return Response(content=twiml, media_type="application/xml")
    
    # Fallback
    twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="fr-FR">Désolé, je n'ai pas compris votre demande.</Say>
</Response>'''
    return Response(content=twiml, media_type="application/xml")
