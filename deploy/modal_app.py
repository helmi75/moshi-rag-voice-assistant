"""Déploiement serverless GPU de l'assistant vocal sur Modal — voix Kyutai 1.6B.

Une seule commande :

    modal deploy deploy/modal_app.py

Ce que ça fait :
  - construit une image GPU (nos dépendances + le paquet `moshi` = TTS 1.6B) ;
  - envoie AUTOMATIQUEMENT votre fichier .env (Secret.from_dotenv) dans le conteneur ;
  - précharge le modèle 1.6B au démarrage du conteneur (pas de cold start pendant l'appel) ;
  - expose l'appli FastAPI (webhook Twilio + WebSocket Media Streams) sur une URL
    publique HTTPS/WSS fournie par Modal — donc AUCUN ngrok nécessaire ;
  - garde 1 conteneur chaud puis scale-to-zero après inactivité (facturation à la
    seconde). Voir docs/MODAL.md.

Prérequis : `pip install modal` puis `modal setup` (une fois), un fichier `.env` à la
racine, et avoir accepté la licence du modèle sur huggingface.co/kyutai/tts-1.6b-en_fr
(mettez HF_TOKEN dans le .env si le modèle est sous conditions).
"""
import os

import modal

APP_NAME = "moshi-voice-assistant"

# GPU : A10G (24 Go) suffit largement pour le 1.6B (~6 Go). L4 = moins cher,
# A100 = plus rapide. Surchargeable via la variable d'env MODAL_GPU au déploiement.
GPU = os.environ.get("MODAL_GPU", "A10G")

# Volume persistant : cache des poids Hugging Face + base SQLite (réservations).
cache = modal.Volume.from_name("moshi-voice-cache", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    # libgomp1 : onnxruntime (VAD/smart-turn) ; libsndfile1 : soundfile ; ffmpeg : audio.
    .apt_install("libgomp1", "libsndfile1", "ffmpeg")
    .pip_install_from_requirements("api/app/requirements.txt")
    # Le 1.6B : paquet officiel Kyutai (tire torch CUDA, sphn, etc.).
    .pip_install("moshi")
    .env(
        {
            # Forcés côté serveur : pipeline streaming + voix Kyutai 1.6B sur GPU.
            "VOICE_MODE": "stream",
            "TTS_PROVIDER": "kyutai",
            "KYUTAI_TTS_DEVICE": "cuda",
            # Caches et base dans le volume persistant.
            "HF_HOME": "/cache/hf",
            "DB_PATH": "/cache/app.db",
        }
    )
    # Notre code applicatif (paquet `app`) → /root/app, importable depuis /root.
    .add_local_dir("api/app", remote_path="/root/app")
)

app = modal.App(APP_NAME)


def _force_server_env() -> None:
    """Impose la config serveur quoi qu'il y ait dans le .env envoyé.

    Le secret .env peut contenir des valeurs pensées pour le déploiement local
    (TTS_PROVIDER vide, un vieux PUBLIC_WS_URL ngrok, VOICE_MODE=gather...). On les
    écrase ici pour que Modal serve TOUJOURS le pipeline streaming + Kyutai 1.6B sur
    GPU, et que l'URL WebSocket soit déduite du domaine modal.run (pas d'ancien ngrok)."""
    os.environ["VOICE_MODE"] = "stream"
    os.environ["TTS_PROVIDER"] = "kyutai"
    os.environ["KYUTAI_TTS_DEVICE"] = "cuda"
    # Supprime un éventuel PUBLIC_WS_URL périmé -> l'app dérive wss://<hôte modal>/ws/voice.
    os.environ.pop("PUBLIC_WS_URL", None)


@app.cls(
    image=image,
    gpu=GPU,
    # Envoie le .env LOCAL comme secret dans le conteneur (clés OpenRouter, Deepgram,
    # Twilio, HF_TOKEN...). C'est le « ça envoie le .env » demandé.
    secrets=[modal.Secret.from_dotenv()],
    volumes={"/cache": cache},
    # 1 conteneur chaud = pas de cold start pendant un appel ; scale-to-zero après 5 min.
    min_containers=int(os.environ.get("MODAL_MIN_CONTAINERS", "1")),
    scaledown_window=300,
    timeout=3600,
)
@modal.concurrent(max_inputs=8)
class VoiceAssistant:
    @modal.enter()
    def warm(self):
        """Précharge le 1.6B au démarrage du conteneur (une fois), pour que le
        premier appel n'attende pas le chargement du modèle."""
        _force_server_env()
        try:
            from app.voice.kyutai_tts import _load_model_and_voice

            _load_model_and_voice()
        except Exception as e:  # ne bloque pas le démarrage HTTP si le préchauffage échoue
            print(f"[warm] préchauffage Kyutai 1.6B échoué (sera chargé au 1er appel): {e}")

    @modal.asgi_app()
    def web(self):
        """Monte l'appli FastAPI existante (webhook Twilio + /ws/voice)."""
        _force_server_env()
        from app.main import app as fastapi_app

        return fastapi_app
