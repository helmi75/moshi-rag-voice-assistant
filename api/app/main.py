from fastapi import FastAPI, Request
from fastapi.responses import Response
from .reservations import create_reservation
from .rag import retrieve_context
import os
import httpx

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

def _call_moshi_api(prompt: str, system: str) -> str:
    """
    Appelle l'API Moshi via Gradio et retourne la réponse textuelle.
    
    Args:
        prompt: Le prompt complet avec contexte
        system: Le message système
        
    Returns:
        La réponse textuelle de Moshi ou un message d'erreur
    """
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
                    return result[0] if isinstance(result[0], str) else str(result[0])
                elif isinstance(result, str):
                    return result
                else:
                    return str(result)
        return f"Désolé, le serveur a retourné une erreur (code {resp.status_code})."
    except httpx.TimeoutException:
        return "Désolé, la requête a pris trop de temps. Le serveur est peut-être occupé."
    except httpx.ConnectError:
        return "Désolé, impossible de se connecter au serveur Moshi. Vérifiez qu'il est démarré."
    except Exception as e:
        return f"Désolé, une erreur s'est produite: {str(e)}"

def _process_user_message(user_text: str, from_number: str) -> str:
    """
    Traite un message utilisateur avec Moshi et retourne la réponse.
    
    Args:
        user_text: Le texte de l'utilisateur
        from_number: Le numéro de l'expéditeur
        
    Returns:
        La réponse générée par Moshi
    """
    if not user_text:
        return "Bonjour ! Je suis Fabieng, l'assistant du Fouquet's. Comment puis-je vous aider ?"
    
    # Récupérer le contexte RAG
    contexts = retrieve_context(user_text, top_k=3)
    
    # Construire le prompt
    system = ("Tu es Fabieng, assistant téléphonique du Fouquet's. Réponds en français soigné, "
              "souriant, efficace. Utilise les informations du restaurant fournies.")
    prompt = system + "\n\nContexte récupéré :\n" + "\n---\n".join(contexts) + "\n\nQuestion : " + user_text
    
    # Appeler Moshi
    text = _call_moshi_api(prompt, system)
    
    # Enregistrer la réservation si mentionnée
    if "réserv" in user_text.lower():
        create_reservation({"raw": user_text, "contact": from_number})
    
    return text

@app.post("/twilio/webhook")
async def twilio_webhook(request: Request):
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
        text = _process_user_message(user_text, from_number)
        
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
            text = _process_user_message(user_text, from_number)

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
