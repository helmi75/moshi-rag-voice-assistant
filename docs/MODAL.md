# Le serveur de voix sur Modal — `moshi-server` (voix Moshi 1.6B, celle d'unmute.sh)

Seul le **serveur de voix** tourne sur Modal (GPU L4, région EU, scale-to-zero). L'application
— webhooks Twilio, Pipecat, base, admin — tourne sur le VPS et n'est que cliente websocket de
ce serveur (`api/app/voice/moshi_server_tts.py`). Le serveur Rust de Kyutai tient le temps
réel (CUDA graphs + batching) là où le chemin PyTorch, essayé en 2026, saccadait.

> L'ancien déploiement de **toute** l'application sur Modal (`deploy/modal_app.py`, voix
> Kyutai en PyTorch) a été retiré le 18/09/2026 — retrouvable au tag `archive/moteurs-locaux`.

## 1. Déployer le serveur

```bash
modal deploy deploy/modal_moshi_server.py
```
La première construction compile le binaire Rust (`cargo install moshi-server@0.6.4`,
~10-15 min) puis embarque le catalogue de voix (`VOICE_FOLDERS`). Modal affiche l'URL
publique, du type `https://<vous>--moshi-server-tts-server.modal.run`.
Prérequis : licence acceptée sur huggingface.co/kyutai/tts-1.6b-en_fr et `HF_TOKEN` dans le
`.env` local (envoyé au conteneur par `Secret.from_dotenv`).

Options, lues au moment du `modal deploy` :

| Variable | Défaut | Rôle |
|---|---|---|
| `MODAL_GPU` | `L4` | GPU (A10G plus rapide, plus cher) — la compute capability CUDA suit |
| `MODAL_REGION` | `eu` | Région ; vide = laisser Modal choisir |
| `MODAL_MIN_CONTAINERS` | `0` | `1` = un GPU toujours chaud (≈ 0,80 $/h), plus de démarrage à froid |
| `MODAL_MAX_CONTAINERS` | `4` | Plafond de GPU simultanés : garde-fou de facture |

## 2. Pointer l'application dessus

Dans le `.env` du VPS :
```
MOSHI_TTS_URL=wss://<vous>--moshi-server-tts-server.modal.run   # https:// accepté aussi
MOSHI_TTS_API_KEY=<openssl rand -hex 32>   # la MÊME valeur que le .env du modal deploy
MOSHI_TTS_VOICE=unmute-prod-website/developpeuse-3.wav
```
Puis `docker compose up -d api`. La supervision (`/supervision`, contrôle « Configuration du
chemin d'appel ») exige `MOSHI_TTS_URL`.

## Jeton

La config publique de Kyutai accepte `public_token`, une clé que tout le monde connaît :
qui trouve l'URL Modal peut faire parler le GPU à nos frais. `deploy/modal_moshi_server.py`
remplace donc `authorized_ids` **au démarrage du conteneur** par `MOSHI_TTS_API_KEY`, lue
dans le `.env` (via `Secret.from_dotenv`) — jamais écrite dans l'image. `modal deploy`
**refuse de partir** si la clé est absente du `.env` ou vaut `public_token`.

Ordre de mise en place (sinon les appels sont muets entre deux étapes) :
1. `openssl rand -hex 32` → `MOSHI_TTS_API_KEY=…` dans le `.env` local **et** celui du VPS ;
2. sur le VPS : `docker compose up -d api` (l'app envoie déjà la nouvelle clé) ;
3. en local : `modal deploy deploy/modal_moshi_server.py` — les appels reprennent au
   premier conteneur démarré avec la clé ;
4. `python scripts/test_moshi_server.py --url <URL> --api-key public_token` doit être
   **refusé**, et `--api-key <clé>` accepté ; `/supervision` → « Jeton du serveur de voix » vert.

Changer de clé plus tard : même ordre.

## 3. Vérifier

Dans les journaux de l'app, à chaque phrase : `moshi-server : … (xF.FF temps réel)` avec
**F ≥ 1**. Le premier appel après 120 s d'inactivité réveille le GPU (55-70 s) : l'accueil
pré-rendu et la musique d'attente couvrent ce délai (`api/app/voice/greeting.py`).

## Voix

Le catalogue servi est fermé (`api/app/voice/voices.py`) et doit correspondre aux dossiers
embarqués dans l'image (`VOICE_FOLDERS` de `deploy/modal_moshi_server.py`) : moshi-server
remplace en silence une voix inconnue par sa voix de repli. Ajouter une voix = les deux
fichiers, puis redéployer.
