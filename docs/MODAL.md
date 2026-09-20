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
| `MODAL_GPU` | `L4` | GPU — la compute capability CUDA suit. **Ne pas descendre sous la L4** : voir « Quel GPU » |
| `MODAL_REGION` | `eu` | Région ; vide = laisser Modal choisir |
| `MODAL_MIN_CONTAINERS` | `0` | `1` = un GPU toujours chaud (≈ 0,80 $/h), plus de démarrage à froid |
| `MODAL_MAX_CONTAINERS` | `2` | Plafond de GPU simultanés : garde-fou de facture (2 GPU = 8 appels) |
| `MODAL_APP_NAME` | `moshi-server` | Déployer un serveur d'ESSAI à côté de la production, sans la remplacer |

## Quel GPU, et combien d'appels par GPU

**La L4 est le GPU le moins cher qui fasse tourner ce serveur.** Essayé le 20/09/2026 :
une T4 ne compile même pas — les noyaux CUDA de moshi-server utilisent des fragments
WMMA en `bf16`, qui n'existent qu'à partir de sm_80 (Ampere), et la T4 est en sm_75
(`nvcc --gpu-architecture=sm_75` → 12 erreurs sur `nv_bfloat16`). Les GPU compatibles
(A10G, A100, L40S, H100…) sont tous plus chers.

Mesuré sur une L4 chaude, avec le banc `scripts/test_moshi_server.py` lancé en parallèle
(20/09/2026, config actuelle) :

| Flux simultanés | Débit par flux | 1er son |
|---|---|---|
| 1 | ×1,78 temps réel | 1,6 s |
| 3 | ×1,71 à 1,75 | 1,3-1,5 s |
| 6 | ×1,65 à 1,74 | 1,3-1,6 s |

Le débit **ne bouge pas** de 1 à 6 flux (19 % d'utilisation GPU, 8,7 Gio de VRAM sur 24) :
le groupage absorbe la charge. D'où `target_inputs = max_inputs = 8` — et les 6 flux
ci-dessus ont bien été servis par **un seul conteneur** (`modal container list`).

⚠️ **Un appel téléphonique compte pour DEUX inputs** : le client pré-ouvre la connexion de
la phrase suivante pour supprimer le blanc entre deux phrases (mesuré le 05/09/2026 :
7 appels réels → 14 inputs). Un conteneur sert donc **4 appels**, et le plafond de 2
conteneurs borne la facture à ~1,6 $/h pour 8 appels simultanés.

Le module de transcription de Kyutai a été retiré de la config le 20/09/2026 : il était
chargé en VRAM à chaque démarrage sans que rien ne l'appelle (la transcription se fait
chez Deepgram). Gain mesuré au réveil : **70 s → 46 s** avant le premier son.

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
**F ≥ 1**. Le premier appel après 120 s d'inactivité réveille le GPU (≈ 46 s) : l'accueil
pré-rendu et la musique d'attente couvrent ce délai (`api/app/voice/greeting.py`).

## Voix

Le catalogue servi est fermé (`api/app/voice/voices.py`) et doit correspondre aux dossiers
embarqués dans l'image (`VOICE_FOLDERS` de `deploy/modal_moshi_server.py`) : moshi-server
remplace en silence une voix inconnue par sa voix de repli. Ajouter une voix = les deux
fichiers, puis redéployer.
