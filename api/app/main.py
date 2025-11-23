from fastapi import FastAPI, Request, BackgroundTasks
from .reservations import create_reservation, list_reservations
from .rag import retrieve_context
import os, httpx

app = FastAPI(title="Fouquets Voice Assistant - API")
MODEL_API = os.getenv("MODEL_API_URL", "http://moshi:8091")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/twilio/webhook")
async def twilio_webhook(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    user_text = form.get('Transcript') or form.get('Body') or ''
    contexts = retrieve_context(user_text, top_k=3)
    system = ("Tu es Fabieng, assistant téléphonique du Fouquet's. Réponds en français soigné, "
              "souriant, efficace. Utilise les informations du restaurant fournies.")
    prompt = system + "\n\nContexte récupéré :\n" + "\n---\n".join(contexts) + "\n\nQuestion : " + user_text

    try:
        resp = httpx.post(f"{MODEL_API}/v1/chat/completions", json={
            "model": "moshi",
            "messages": [
                {"role":"system","content": system},
                {"role":"user","content": prompt}
            ],
            "max_tokens": 300
        }, timeout=60.0)
        if resp.status_code == 200:
            data = resp.json()
            text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        else:
            text = "Désolé, je n'ai pas pu traiter la demande pour le moment."
    except Exception as e:
        text = "Erreur interne lors de l'appel au modèle: " + str(e)

    if "réserv" in user_text.lower():
        create_reservation({"raw": user_text, "contact": form.get('From', '')})

    return {"reply": text}
