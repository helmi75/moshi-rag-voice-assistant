"""Déploiement serverless GPU du serveur Rust `moshi-server` (voix Moshi 1.6B + STT) sur Modal.

C'est LE serveur de production de Kyutai (celui d'unmute.sh) : voix Moshi 1.6B servie en
temps réel (CUDA graphs + batching), fluide là où le chemin PyTorch sacade. L'application
(sur le serveur 24/7) s'y connecte en simple cliente websocket via `MOSHI_TTS_URL` (TTS)
et `MOSHI_STT_URL` (STT, défaut = même serveur).

Le même process moshi-server sert DEUX modules sur deux routes (le serveur Rust accepte
une `HashMap` de modules dans un seul config.toml — commentaire officiel de config-tts.toml :
« the server can run STT at the same time ») :
  - TTS : [modules.tts_py] type="Py"      -> /api/tts_streaming   (voix Moshi 1.6B)
  - STT : [modules.asr]   type="BatchedAsr" -> /api/asr-streaming (kyutai/stt-1b-en_fr)
Les deux modèles coexistent en VRAM sur la L4 (24 Go) ; le batch ASR est ramené 64->8
pour tenir avec le TTS (batch 8). Si un cold start manque de VRAM (logs Modal), baisser
encore le batch ASR ou passer audio_codebooks 32->20 (réglage prod d'unmute).

Une commande :

    modal deploy deploy/modal_moshi_server.py

Recette calquée sur la Dockerfile publique d'unmute (services/moshi-server) :
  - base CUDA devel + Rust + `cargo install --features cuda moshi-server@0.6.4` (au build) ;
  - config TTS publique (configs/config-tts.toml de delayed-streams-modeling) + blocs ASR
    de config-stt-en_fr-hf.toml concaténés au build, token `public_token` ;
  - les modèles (TTS 1.6B, STT 1B) sont téléchargés au 1er démarrage dans un volume
    persistant (HF cache) ;
  - le serveur écoute sur 8080, exposé en HTTPS/WSS par Modal (@modal.web_server).

Prérequis : accepter la licence sur huggingface.co/kyutai/tts-1.6b-en_fr et fournir un
token HF (secret Modal `huggingface` avec HF_TOKEN, ou .env via Secret.from_dotenv).

⚠️ 1er déploiement : surveiller les logs de build (cargo install ~10-15 min la 1re fois)
puis de démarrage.
  - La compilation des kernels CUDA (candle) au build N'A PAS de GPU : on force donc
    CUDA_COMPUTE_CAP (mappée sur MODAL_GPU) pour éviter l'appel à `nvidia-smi` — sinon
    le build échoue avec « `nvidia-smi` failed ». (corrigé ci-dessous)
  - Le binaire embarque Python (pyo3/tts_py) : il se lie à libpython3.12 au build.
    add_python fournit déjà libpython3.12.so dans /usr/local/lib ; on expose ce dossier
    via LIBRARY_PATH, sinon l'édition de liens échoue (« unable to find library
    -lpython3.12 »). (corrigé)
  - Points restants à ajuster si besoin : l'adresse/port de bind (on suppose 0.0.0.0:8080)
    et le chemin/nom exact du fichier de config.
"""
import os

import modal

APP_NAME = "moshi-server"
PORT = 8080

# L4 : fluide en Rust d'après Kyutai. Surchargeable via MODAL_GPU (A10G plus rapide).
GPU = os.environ.get("MODAL_GPU", "L4")

# Région Modal. Co-localiser le GPU avec l'app (et près des clients FR) supprime
# l'aller-retour transatlantique ressenti pendant l'appel : chaque mot fait un
# aller-retour app<->TTS, donc un serveur aux US depuis une app en Europe ajoute
# ~150 ms par échange. Défaut « eu ». Vider MODAL_REGION (MODAL_REGION="") pour
# laisser Modal choisir — utile si la sélection de région n'est pas offerte par le
# plan. Passer une région précise (ex. « eu-west-1 ») est aussi accepté.
MODAL_REGION = os.environ.get("MODAL_REGION", "eu").strip()

# Compute capability CUDA par GPU. moshi-server (candle-kernels) compile ses kernels CUDA
# AU BUILD de l'image, où AUCUN GPU n'est présent (`nvidia-smi` absent -> le build plante).
# On fournit donc la valeur en dur via CUDA_COMPUTE_CAP, ce qui évite l'appel à nvidia-smi.
# Elle DOIT correspondre au GPU d'exécution (kernels non rétro-compatibles vers le bas).
_COMPUTE_CAP = {
    "T4": "75", "L4": "89", "A10G": "86", "A100": "80",
    "A100-40GB": "80", "A100-80GB": "80", "L40S": "89", "H100": "90",
}
CUDA_COMPUTE_CAP = _COMPUTE_CAP.get(GPU.split(":")[0].strip(), "89")

# Version du serveur Rust (pinnée comme dans le script officiel d'unmute).
MOSHI_SERVER_VERSION = "0.6.4"

# Config TTS publique (paire avec le protocole du client MoshiServerTTSService).
CONFIG_URL = (
    "https://raw.githubusercontent.com/kyutai-labs/delayed-streams-modeling/"
    "main/configs/config-tts.toml"
)
# Config STT publique (kyutai/stt-1b-en_fr, français natif + VAD sémantique). On n'en
# garde que les blocs [modules.asr]* (les clés top-level static_dir/authorized_ids/... sont
# déjà dans config-tts.toml) qu'on concatène au config TTS au build.
CONFIG_STT_URL = (
    "https://raw.githubusercontent.com/kyutai-labs/delayed-streams-modeling/"
    "main/configs/config-stt-en_fr-hf.toml"
)

# Cache persistant des poids Hugging Face (évite un re-téléchargement à chaque cold start).
hf_cache = modal.Volume.from_name("moshi-server-hf-cache", create_if_missing=True)

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12"
    )
    .apt_install(
        "curl", "build-essential", "ca-certificates", "libssl-dev", "git",
        "pkg-config", "cmake", "wget",
    )
    # Rust (rustup) — pour compiler moshi-server.
    .run_commands("curl https://sh.rustup.rs -sSf | sh -s -- -y")
    # Le module `tts_py` du serveur (config-tts.toml : [modules.tts_py] type="Py")
    # exécute un script Python `tts.py` DANS le serveur (via pyo3). Ses dépendances sont
    # celles du projet `tts-python` de moshi-server (rust/moshi-server/pyproject.toml) :
    # moshi==0.2.13, setuptools, xformers, pydantic, julius, torchaudio. Sans elles, le
    # serveur démarre puis plante (« ModuleNotFoundError: No module named 'pydantic' »).
    # `moshi` fournit aussi la libpython pointée par LD_LIBRARY_PATH au démarrage.
    .pip_install(
        "moshi", "huggingface_hub",
        "setuptools", "xformers", "pydantic", "julius", "torchaudio",
    )
    # Variables de BUILD :
    #  - CUDA_COMPUTE_CAP : compile les kernels candle sans GPU (sinon appel à
    #    `nvidia-smi`, absent du builder -> échec).
    #  - LIBRARY_PATH : ajoute /usr/local/lib au chemin de recherche de l'éditeur de
    #    liens pour trouver libpython3.12 (le binaire embarque Python via pyo3/tts_py).
    # NB : add_python fournit déjà libpython3.12.so (lien) + libpython3.12.so.1.0 dans
    # /usr/local/lib ; il suffit d'exposer ce dossier via LIBRARY_PATH (ci-dessus) pour
    # que l'éditeur de liens résolve -lpython3.12. Pas de symlink à créer.
    .env({"CUDA_COMPUTE_CAP": CUDA_COMPUTE_CAP, "LIBRARY_PATH": "/usr/local/lib"})
    # Compile et installe le binaire moshi-server (feature CUDA). Long la 1re fois,
    # mais mis en cache dans la couche d'image (pas refait à chaque déploiement).
    .run_commands(
        "bash -lc '. $HOME/.cargo/env && "
        f"CARGO_TARGET_DIR=/app/target cargo install --features cuda "
        f"moshi-server@{MOSHI_SERVER_VERSION}'",
    )
    # Récupère la config TTS publique + crée le dossier static attendu par le serveur.
    .run_commands(
        "mkdir -p /root/configs /root/static",
        f"wget -qO /root/configs/config-tts.toml {CONFIG_URL}",
        # La config amont télécharge TOUTES les voix (kyutai/tts-voices, ~901 fichiers)
        # -> on sature la limite d'API HF (429 Too Many Requests) au démarrage et chaque
        # cold start est lent. On restreint le glob au seul dossier de voix utilisé
        # (unmute-prod-website : contient default_voice + ex04_narration_longform_00001).
        # Élargir ce glob si on veut d'autres voix du dépôt.
        r"sed -i 's#tts-voices/\*\*/#tts-voices/unmute-prod-website/#' "
        "/root/configs/config-tts.toml",
        # Ajoute le module STT (ASR) au MÊME config : on télécharge config-stt-en_fr-hf.toml
        # et on n'en garde que les tables [modules.asr]* (à partir de la 1re), que l'on
        # concatène. Les clés top-level (static_dir, authorized_ids...) sont identiques et
        # déjà présentes -> ne pas les redupliquer.
        f"wget -qO /root/configs/config-stt.toml {CONFIG_STT_URL}",
        "printf '\\n' >> /root/configs/config-tts.toml",
        r"sed -n '/^\[modules\.asr\]/,$p' /root/configs/config-stt.toml "
        ">> /root/configs/config-tts.toml",
        # VRAM L4 partagée avec le TTS (batch 8) : on ramène le batch ASR 64 -> 8. Le TTS
        # a déjà batch_size = 8, donc ce remplacement ne touche QUE la ligne ASR (64).
        "sed -i 's/^batch_size = 64$/batch_size = 8/' /root/configs/config-tts.toml",
    )
)

app = modal.App(APP_NAME)


@app.function(
    image=image,
    gpu=GPU,
    # Épingle le GPU dans la région choisie (EU par défaut) pour couper la latence
    # réseau app<->TTS. None (MODAL_REGION vide) = pas de contrainte, Modal choisit.
    region=MODAL_REGION or None,
    volumes={"/root/.cache/huggingface": hf_cache},
    # Token HF pour télécharger le modèle 1.6B (secret Modal OU .env local).
    secrets=[modal.Secret.from_dotenv()],
    # scale-to-zero par défaut (on ne paie le GPU que pendant les appels). Passer
    # MODAL_MIN_CONTAINERS=1 pour garder une box chaude aux heures d'ouverture.
    min_containers=int(os.environ.get("MODAL_MIN_CONTAINERS", "0")),
    scaledown_window=120,
    timeout=3600,
)
# Le batching (jusqu'à batch_size=8 connexions simultanées) est géré EN INTERNE par
# moshi-server ; Modal proxifie simplement le port. Pas besoin de @modal.concurrent ici.
@modal.web_server(PORT, startup_timeout=900)
def tts_server():
    """Démarre moshi-server (non bloquant) ; Modal proxifie le port en HTTPS/WSS."""
    import subprocess
    import sysconfig

    env = dict(os.environ)
    # tts_py se lie à la libpython -> LD_LIBRARY_PATH sur le LIBDIR Python (cf. script
    # officiel start_moshi_server_public.sh).
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    env["LD_LIBRARY_PATH"] = libdir + ":" + env.get("LD_LIBRARY_PATH", "")
    # Certaines libs HF lisent HUGGING_FACE_HUB_TOKEN plutôt que HF_TOKEN.
    if env.get("HF_TOKEN") and not env.get("HUGGING_FACE_HUB_TOKEN"):
        env["HUGGING_FACE_HUB_TOKEN"] = env["HF_TOKEN"]

    subprocess.Popen(
        [
            "/root/.cargo/bin/moshi-server",
            "worker",
            "--config", "/root/configs/config-tts.toml",
            "--port", str(PORT),
        ],
        env=env,
        cwd="/root",
    )
