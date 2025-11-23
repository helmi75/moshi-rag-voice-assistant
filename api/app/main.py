from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
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
    form = await request.form()
    user_text = form.get('Transcript') or form.get('Body') or ''
    contexts = retrieve_context(user_text, top_k=3)
    system = ("Tu es Fabieng, assistant téléphonique du Fouquet's. Réponds en français soigné, "
              "souriant, efficace. Utilise les informations du restaurant fournies.")
    prompt = system + "\n\nContexte récupéré :\n" + "\n---\n".join(contexts) + "\n\nQuestion : " + user_text

    # Le serveur Moshi utilise Gradio qui expose une API
    # On essaie d'abord l'API Gradio standard, puis on adapte si nécessaire
    try:
        # Gradio expose généralement l'API sur /api/predict
        # Format: {"data": [input_data], "fn_index": 0}
        # Pour Moshi, on envoie le texte de l'utilisateur
        resp = httpx.post(
            f"{MODEL_API}/api/predict",
            json={
                "data": [prompt, system],  # Texte utilisateur et système
                "fn_index": 0
            },
            timeout=120.0  # Timeout plus long pour la génération
        )
        
        if resp.status_code == 200:
            data = resp.json()
            # Gradio retourne généralement {"data": [result]}
            if "data" in data and len(data["data"]) > 0:
                # Le résultat peut être une liste ou une string
                result = data["data"][0]
                if isinstance(result, list) and len(result) > 0:
                    text = result[0] if isinstance(result[0], str) else str(result[0])
                elif isinstance(result, str):
                    text = result
                else:
                    text = str(result)
            else:
                text = "Réponse reçue mais format inattendu."
        else:
            # Si l'API Gradio ne fonctionne pas, on essaie une approche alternative
            # ou on retourne un message d'erreur
            text = f"Désolé, le serveur a retourné une erreur (code {resp.status_code})."
            
    except httpx.TimeoutException:
        text = "Désolé, la requête a pris trop de temps. Le serveur est peut-être occupé."
    except httpx.ConnectError:
        text = "Désolé, impossible de se connecter au serveur Moshi. Vérifiez qu'il est démarré."
    except Exception as e:
        text = f"Erreur interne lors de l'appel au modèle: {str(e)}"

    if "réserv" in user_text.lower():
        create_reservation({"raw": user_text, "contact": form.get('From', '')})

    return {"reply": text}
