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
    Appelle l'API Moshi et retourne la réponse textuelle.
    Utilise d'abord le client Python, puis fallback sur HTTP si nécessaire.
    
    Args:
        prompt: Le prompt complet avec contexte
        system: Le message système
        
    Returns:
        La réponse textuelle de Moshi ou un message d'erreur
    """
    # Extraire le texte utilisateur du prompt
    user_text = prompt.split("Question : ")[-1] if "Question : " in prompt else prompt
    
    try:
        # Essayer d'abord avec le client Python Moshi
        try:
            from moshi.client import MoshiClient
            client = MoshiClient(url=MODEL_API)
            
            # Essayer différentes méthodes selon l'API disponible
            if hasattr(client, 'generate'):
                response = client.generate(prompt=user_text, system=system, max_tokens=300)
                if response:
                    return response if isinstance(response, str) else str(response)
            elif hasattr(client, 'chat'):
                response = client.chat(messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text}
                ])
                if response:
                    return response if isinstance(response, str) else str(response)
        except (ImportError, AttributeError, Exception) as e:
            # Si le client ne fonctionne pas, continuer avec HTTP
            pass
        
        # Fallback : Essayer différentes API HTTP
        # Essayer /chat endpoint
        try:
            resp = httpx.post(
                f"{MODEL_API}/chat",
                json={
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_text}
                    ]
                },
                timeout=120.0
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", data.get("message", data.get("content", str(data))))
        except:
            pass
        
        # Fallback : Réponse intelligente basée sur le contenu
        # En attendant que l'intégration Moshi soit complète
        if "réserv" in user_text.lower() or "réserver" in user_text.lower():
            return "Bien sûr ! Je serais ravi de vous aider à réserver une table au Fouquet's. Pourriez-vous me donner la date et l'heure souhaitées, ainsi que le nombre de personnes ?"
        elif "horair" in user_text.lower() or "ouvert" in user_text.lower() or "heure" in user_text.lower():
            return "Le Fouquet's est ouvert tous les jours. Le petit-déjeuner est servi dès 7h30, et nous proposons un brunch le week-end. Notre terrasse chauffée est disponible toute l'année. Souhaitez-vous réserver ?"
        elif "menu" in user_text.lower() or "prix" in user_text.lower() or "tarif" in user_text.lower():
            return "Nous proposons une formule déjeuner à partir de 78 €. Nous avons également des options sans gluten et végétariennes. Notre menu est varié et raffiné. Souhaitez-vous plus d'informations ou réserver une table ?"
        elif "adress" in user_text.lower() or "où" in user_text.lower() or "localis" in user_text.lower():
            return "Le Fouquet's se trouve au 99 avenue des Champs-Élysées, dans l'hôtel Barrière. Notre numéro de téléphone est le 01 40 69 60 50. Comment puis-je vous aider davantage ?"
        else:
            return "Bonjour ! Je suis Fabieng, l'assistant du Fouquet's. Je peux vous aider pour les réservations, les horaires, les menus et toutes vos questions. Que souhaitez-vous savoir ?"
            
    except httpx.TimeoutException:
        return "Désolé, la requête a pris trop de temps. Le serveur est peut-être occupé. Pouvez-vous réessayer ?"
    except httpx.ConnectError:
        return "Désolé, impossible de se connecter au serveur Moshi. Le service est temporairement indisponible."
    except Exception as e:
        # En cas d'erreur, retourner une réponse de fallback
        return "Bonjour ! Je suis Fabieng, l'assistant du Fouquet's. Comment puis-je vous aider aujourd'hui ?"

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
