# Déploiement — hébergement 24/7 sur VPS (production)

> But : faire tourner l'app **en continu sur un serveur cloud EU**, avec un **domaine
> stable + HTTPS**, pour que la ligne téléphonique soit disponible 24/7 **sans dépendre
> d'un PC ni de ngrok**. Modal reste dédié au TTS GPU (voix Moshi). C'est la Phase 2 de
> `docs/PASSATION.md`.
>
> Architecture : `Twilio → https://votre-domaine (Caddy HTTPS → api:8000, FastAPI+Pipecat,
> SQLite) → websocket → Modal GPU EU (moshi-server)`.

## Prérequis

1. **VPS** en **région EU** (Hostinger ou équivalent), Ubuntu 22.04+, 2 vCPU / 4 Go conseillés.
2. **Domaine ou sous-domaine** (ex. `assistant.mondomaine.fr`) dont l'enregistrement DNS **A**
   pointe sur l'IP du VPS. Indispensable : Twilio exige un webhook stable et Let's Encrypt ne
   certifie pas une IP nue.
3. **Clés fraîches** (rotation) : Twilio auth token, Deepgram, OpenRouter — ne jamais réutiliser
   une clé qui a été exposée. Le serveur Modal doit être déployé (voir `docs/MODAL.md`).

## 1 — VPS + Docker

```bash
# sur le VPS, en root ou sudo
apt-get update && apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh          # Docker + Compose plugin
docker --version && docker compose version       # vérifier
```

## 2 — DNS

Créer un enregistrement **A** : `assistant.mondomaine.fr → <IP du VPS>`. Vérifier la propagation :
```bash
dig +short assistant.mondomaine.fr               # doit renvoyer l'IP du VPS
```
Ouvrir les ports **80** et **443** (Let's Encrypt valide via le port 80).

## 3 — Cloner + configurer

```bash
git clone https://github.com/helmi75/moshi-rag-voice-assistant.git
cd moshi-rag-voice-assistant
cp env.example .env
nano .env
```

`.env` de production — valeurs minimales :
```
VOICE_MODE=stream
SITE_ADDRESS=assistant.mondomaine.fr             # -> Caddy fait le HTTPS tout seul
PUBLIC_WS_URL=wss://assistant.mondomaine.fr/ws/voice
API_REQUIREMENTS=requirements-prod.txt           # image légère (sans torch)

STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=<clé fraîche>

TTS_PROVIDER=moshi_server
MOSHI_TTS_URL=wss://<vous>--moshi-server-tts-server.modal.run
MOSHI_TTS_API_KEY=public_token
MOSHI_TTS_VOICE=unmute-prod-website/developpeuse-3.wav

LLM_MODEL=google/gemini-2.5-flash
OPENROUTER_API_KEY=<clé fraîche>

TWILIO_ACCOUNT_SID=<...>
TWILIO_AUTH_TOKEN=<clé fraîche>
TWILIO_NUMBER=<+33...>

# Admin
ADMIN_EMAIL=vous@exemple.fr
ADMIN_PASSWORD=<mot de passe fort>
SESSION_SECRET=<openssl rand -hex 32>
SESSION_SECURE=1                                 # cookies HTTPS-only (domaine en place)

# Naturalité (défauts déjà validés)
USER_TURN_STOP_TIMEOUT=1.0
VAD_CONFIDENCE=0.85
VAD_STOP_SECS=0.4
```

## 4 — Démarrer

```bash
docker compose up -d --build
curl -s https://assistant.mondomaine.fr/health   # attendu: {"status":"ok",...} en HTTPS
```
Au 1er démarrage, Caddy obtient le certificat Let's Encrypt (quelques secondes). `restart:
unless-stopped` est déjà en place : la pile **remonte seule** après un reboot.

## 5 — Pointer Twilio

Dans la console Twilio, le numéro → **Voice → A call comes in → Webhook** :
```
https://assistant.mondomaine.fr/twilio/voice   (HTTP POST)
```
Fini ngrok. L'URL ne changera plus.

## 6 — Modal (TTS GPU)

Le serveur Modal est déjà en **région EU** par défaut (`MODAL_REGION=eu`). Redéployer si besoin :
```bash
modal deploy deploy/modal_moshi_server.py
```
**Cold start / GPU chaud** (`MODAL_MIN_CONTAINERS`, à décider selon le budget) :
- **Scale-to-zero** (défaut, recommandé au lancement) : `0`. Gratuit à vide ; le 1er appel après
  une pause attend ~70 s, **couvert par l'accueil pré-enregistré + la musique d'attente**.
- **GPU chaud** : `1` → 0 cold start, mais ~250-290 €/mois (L4 24/7). À activer quand un client
  payant le justifie : `MODAL_MIN_CONTAINERS=1 modal deploy deploy/modal_moshi_server.py`.

## Vérification (bout en bout)

1. `curl https://assistant.mondomaine.fr/health` → 200, certificat HTTPS valide.
2. Admin : `https://assistant.mondomaine.fr/admin/login` → connexion OK, design en place.
3. **Appel réel** sur le numéro Twilio : accueil → conversation → **réservation enregistrée** ;
   transcript propre dans l'admin.
4. **Résilience (le critère qui compte)** : `sudo reboot` → au retour, `docker compose ps` montre
   `api` et `caddy` **Up** sans rien lancer, et la ligne re-répond. Plus aucune dépendance au PC.

## Exploitation

- **Logs** : `docker compose logs -f api` (appels, transcripts, coûts).
- **Sauvegarde** de la base (réservations, comptes) — le volume `api_data` contient le SQLite :
  ```bash
  docker compose cp api:/app/data/app.db ./backup-$(date +%F).db
  ```
  (à planifier en cron pour un vrai client.)
- **Mise à jour** : `git pull && docker compose up -d --build`.

## Rollback

Le PC local + ngrok restent un repli valable : repointer le webhook Twilio sur l'ancienne URL
ngrok suffit à revenir en arrière en < 1 min. Aucun changement n'est destructif — le `Caddyfile`
reste rétro-compatible en local (`SITE_ADDRESS` par défaut `:80`).
