# Passation — Assistant vocal téléphonique (SaaS accueil resto, voix Moshi)

> Document de contexte pour reprendre le projet dans une nouvelle session (Claude Code
> connecté au WSL, avec Docker / ngrok / modal CLI). Objectif : autonomie — lancer et
> vérifier les commandes directement, sans copier-coller les sorties à chaque fois.
> Repo cloné dans `~/moshi-rag-voice-assistant`, branche de travail `claude/moshi-server-0w8lsv`.

## 1. But du projet

SaaS quasi-zéro-budget : un assistant téléphonique IA qui répond 24/7 aux appels
(restaurants d'abord, médecins ensuite), en **français**, et prend les réservations.
Priorité : la **voix** doit être **fluide** et la latence faible. Voix retenue :
**Moshi 1.6B de Kyutai** (la vraie voix d'unmute.sh), timbre « Développeuse ».

## 2. Architecture

**Actuelle (validée, fonctionne de bout en bout) :**

```
Twilio ──webhook + media stream──► APP FastAPI + Pipecat (EN LOCAL, Docker sur le WSL)
                                    • STT Deepgram + LLM OpenRouter (google/gemini-2.5-flash)
                                    • BDD SQLite (réservations)
                                         │ websocket TTS (client)
                                         ▼
                                    MODAL GPU serverless (scale-to-zero)
                                    • moshi-server (Rust) : voix Moshi 1.6B, TTS fluide
```

ngrok expose l'app locale à Twilio.

**Cible (plan) :**
- App → migrer sur un serveur **CPU 24/7 en EU (Hostinger)**. Pas sur Modal.
- Modal = **uniquement** le TTS GPU.
- Tout en région **EU** pour couper la latence réseau.

## 3. État actuel — ce qui marche

- ✅ **moshi-server déployé sur Modal** (app Modal = `moshi-server`).
  - URL : `https://helmi75--moshi-server-tts-server.modal.run` (WSS pour le TTS).
  - Déploiement : `deploy/modal_moshi_server.py`. GPU L4, scale-to-zero (scaledown 120 s),
    token serveur `public_token`, voix par défaut `unmute-prod-website/developpeuse-3.wav`,
    batch_size 8.
- ✅ **Voix validée FLUIDE** : à chaud, génère ~7 s d'audio en ~2-5 s (x1,4-1,7 temps réel).
- ✅ **Appel Twilio réel réussi** : voix Développeuse, conversation, et **réservation
  enregistrée en base** (client « Robert », 5 pers, 20:00). Le produit tourne.
- ✅ **Client TTS Pipecat** : `api/app/voice/moshi_server_tts.py` (client websocket ;
  protocole msgpack : envoie `{"type":"Text","text":mot}` par mot puis `{"type":"Eos"}`,
  reçoit `{"type":"Audio","pcm":[float32]}` à 24000 Hz).
- ✅ **Script de test direct** (sans Twilio) : `scripts/test_moshi_server.py`.

## 4. Config `.env` (déjà en place sur le WSL)

> Les secrets vont dans `.env` (gitignored), **jamais** dans `env.example`.

```
VOICE_MODE=stream
TTS_PROVIDER=moshi_server
MOSHI_TTS_URL=wss://helmi75--moshi-server-tts-server.modal.run
MOSHI_TTS_API_KEY=public_token
MOSHI_TTS_VOICE=unmute-prod-website/developpeuse-3.wav
PUBLIC_WS_URL=wss://<URL-NGROK>/ws/voice      # ⚠️ change à CHAQUE redémarrage de ngrok
LLM_MODEL=google/gemini-2.5-flash
TWILIO_NUMBER=<numéro Twilio>
# + clés présentes : OPENROUTER_API_KEY, DEEPGRAM_API_KEY, TWILIO_ACCOUNT_SID,
#   TWILIO_AUTH_TOKEN, HF_TOKEN
```

Le tenant de démo est (re)créé automatiquement au démarrage avec `TWILIO_NUMBER`
(seed dans `api/app/tenants.py` ; `get_by_phone(To)` route l'appel vers le bon tenant).

## 5. Commandes pour lancer / vérifier

```bash
# App locale (garder le terminal ouvert)
cd ~/moshi-rag-voice-assistant && docker compose up --build api
curl -s http://localhost:8000/health          # attendu : "voice_mode":"stream"

# Tunnel public (2e terminal — URL éphémère)
ngrok http 8000
# → mettre PUBLIC_WS_URL=wss://<ngrok>/ws/voice dans .env, PUIS recréer le conteneur :
docker compose up --force-recreate api

# Pointer Twilio sur l'app
set -a; source .env; set +a
python3 scripts/twilio_setup_number.py --webhook https://<ngrok>/twilio/webhook

# Déployer / réveiller le TTS Modal
modal deploy deploy/modal_moshi_server.py
python scripts/test_moshi_server.py --url https://helmi75--moshi-server-tts-server.modal.run

# Voir les réservations
curl -s http://localhost:8000/tenants/1/reservations | python3 -m json.tool

# Tests unitaires (aucun réseau, tout mocké)
cd api && python -m pytest tests/ -v
```

**Endpoints utiles** : `GET /health`, `POST /twilio/webhook` (voix + SMS),
`GET /tenants/{id}/reservations`, WS `/ws/voice` (mode stream).

## 6. Problème principal restant = latence (2 causes, pas la vitesse du modèle)

- **a) Cold start** : serveur endormi → 1er mot en 14-56 s (rechargement du modèle). Le
  `x0.11` du script de test vient de là (les ~50 s de reload sont comptées dans le total).
  ⚠️ Toujours **réveiller le TTS** (lancer `test_moshi_server.py`) **avant** un appel.
- **b) Réseau** : app en France ↔ GPU aux US = aller-retour transatlantique par mot.
  → **Fix en cours** : région EU (voir §7).

Une fois **à chaud**, moshi-server EST temps réel (logs serveur : ~7 s d'audio en 2-5 s).

## 7. Tâche immédiate à faire (codée, pas encore redéployée)

Le commit `6d1cab8` épingle le GPU Modal en **région EU** (variable `MODAL_REGION`,
défaut `eu`) dans `deploy/modal_moshi_server.py`. À faire :

1. `git pull` (déjà poussé sur `origin/claude/moshi-server-0w8lsv`).
2. `modal deploy deploy/modal_moshi_server.py`
   - ⚠️ Le 1er cold start EU peut re-télécharger le modèle (une seule fois).
   - Si erreur « region … not available / plan » → relancer :
     `MODAL_REGION="" modal deploy deploy/modal_moshi_server.py` et le signaler.
3. Réveiller (test script ×2), puis **rappeler le numéro Twilio** et mesurer si le délai
   de réponse a baissé. Chercher dans les logs de l'app :
   `moshi-server : N.NNs d'audio en M.MMs (xF.FF temps réel)`.

## 8. Backlog priorisé

- **Phase 3 — greeting pré-enregistré + warmup** (le plus gros gain UX) : au décroché,
  jouer un WAV « Bonjour, restaurant X, un instant » **déjà enregistré** en voix
  Développeuse (latence 0) et lancer en parallèle un **ping de warmup** vers moshi-server,
  pour que le client n'entende jamais le blanc du cold start. Injecter les frames du WAV
  dans `api/app/voice/bot.py::run_bot` au lieu de `TTSSpeakFrame(greeting)`. Prévoir
  `assets/hold_music.wav` en secours si la 1re réponse live tarde.
- **Phase 2 — migration Hostinger** : app sur CPU 24/7 EU, image légère (sans torch/moshi),
  BDD sur volume persistant, Twilio pointe sur le domaine Hostinger. Modal reste TTS-only.
- **Scaling** : moshi-server fait du batching (batch_size 8) → un seul L4 EU chaud peut
  servir ~8-16 appels simultanés de restos différents. Début = scale-to-zero + warmup ;
  croissance = 1 GPU EU chaud partagé (~250-290 €/mois amorti sur tous les restos, 0 cold start).

## 9. Coûts constatés (rentable)

Par appel ~2-3 min : LLM ~0,5 c (négligeable), STT ~1 c, Twilio ~2-3 c, GPU Modal ~2-4 c à
chaud (~10 c si le cold start est compté). **Total ~5-8 c à chaud.** À 40 €/mois/resto pour
~150 appels → marge > 70 %. Seul vrai levier de coût = le GPU (cold start + idle), traité par
le warmup et la région EU.

## 10. Autre app Modal à ne pas confondre

Il existe une 2e app Modal `moshi-voice-assistant` (`deploy/modal_app.py`) = **ancien
monolithe** qui hébergeait toute l'app sur GPU. **Legacy, à abandonner** (Phase 2). On peut
la stopper pour ne pas gaspiller du GPU : `modal app stop moshi-voice-assistant`.

## 11. Contraintes / règles

- ⚠️ **Sécurité** : des clés (Twilio auth token, Cartesia, Deepgram, Anthropic, OpenRouter)
  ont été exposées en clair et sont **compromises** → à **faire tourner (rotation)** côté
  fournisseurs. Secrets dans `.env` (gitignored), jamais dans `env.example`. Ne jamais
  afficher les **valeurs** des clés (seulement leur présence).
- Développer et pousser **uniquement** sur la branche `claude/moshi-server-0w8lsv`.
- **Ne pas créer de Pull Request** sans demande explicite.
