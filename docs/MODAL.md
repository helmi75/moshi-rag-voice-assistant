# Déploiement serverless GPU sur Modal — voix Moshi 1.6B (celle d'unmute.sh)

## ⭐ Voie recommandée : serveur Rust `moshi-server` (voix fluide)

Le chemin PyTorch décrit plus bas (`TTS_PROVIDER=kyutai`, tout dans un conteneur) **reste
sous le temps réel sur L4/T4 → voix saccadée**. La voie de production de Kyutai est le
**serveur Rust `moshi-server`** (CUDA graphs + batching, fluide). L'app devient simple
cliente websocket (`TTS_PROVIDER=moshi_server`).

**1. Déployer le serveur TTS sur Modal :**
```bash
modal deploy deploy/modal_moshi_server.py
```
La 1re construction compile le binaire Rust (`cargo install moshi-server@0.6.4`, ~10-15 min).
Modal affiche ensuite l'URL publique du serveur, ex. :
`https://<vous>--moshi-server-tts-server.modal.run`
(Prérequis : licence acceptée sur huggingface.co/kyutai/tts-1.6b-en_fr + `HF_TOKEN` dans le `.env`.)

**2. Pointer l'app dessus** — dans le `.env` du serveur applicatif :
```
TTS_PROVIDER=moshi_server
MOSHI_TTS_URL=wss://<vous>--moshi-server-tts-server.modal.run   # l'URL ci-dessus (https ok)
MOSHI_TTS_API_KEY=public_token
MOSHI_TTS_VOICE=unmute-prod-website/ex04_narration_longform_00001.wav
```

**3. Vérifier** : dans les logs de l'app, `moshi-server : … (xF.FF temps réel)` avec **F ≥ 1**
= voix fluide. Le serveur scale-to-zero (payé seulement pendant les appels).

> Architecture cible (voir `docs/ARCHI.md`) : l'app (webhooks + BDD + orchestration) tourne
> sur un serveur CPU 24/7 ; SEUL ce `moshi-server` est sur Modal GPU.

---

## (Historique) Voix Kyutai 1.6B en PyTorch dans l'app

Ce guide déploie **toute l'appli** (webhook Twilio + WebSocket Media Streams + cerveau
LLM/réservations) sur **Modal**, avec le TTS **Kyutai 1.6B** (`kyutai/tts-1.6b-en_fr`)
sur GPU serverless. Modal fournit l'URL publique → **pas de ngrok**, scale-to-zero,
facturation à la seconde.

## Pourquoi Modal plutôt que Vast.ai
- **Une commande** : `modal deploy` construit l'image, envoie le `.env`, expose une URL
  publique HTTPS/WSS stable.
- **Scale-to-zero** : vous ne payez le GPU que quand ça tourne.
- **Pas de ngrok** : Twilio appelle directement l'URL `…modal.run`.

## Prérequis (une seule fois)
1. **Compte Modal** + CLI :
   ```bash
   pip install modal python-dotenv   # python-dotenv : requis pour envoyer le .env
   modal setup                       # ouvre le navigateur pour authentifier
   ```
   > Si le navigateur ne s'ouvre pas (WSL/serveur), `modal setup` affiche une URL à
   > ouvrir manuellement, puis écrit le token dans `~/.modal.toml`. C'est normal.
2. **`.env` à la racine** (copié de `env.example`, rempli). Doivent y figurer au moins :
   ```
   OPENROUTER_API_KEY=...
   DEEPGRAM_API_KEY=...
   TWILIO_ACCOUNT_SID=...
   TWILIO_AUTH_TOKEN=...
   TWILIO_NUMBER=+1...
   # HF_TOKEN=...   # si le modèle Kyutai est sous conditions (voir point 3)
   ```
   Pas besoin d'y mettre `VOICE_MODE`, `TTS_PROVIDER`, `KYUTAI_TTS_DEVICE`, `HF_HOME`,
   `DB_PATH` : ils sont forcés côté serveur par `deploy/modal_app.py`.
3. **Licence du modèle** : ouvrez [huggingface.co/kyutai/tts-1.6b-en_fr](https://huggingface.co/kyutai/tts-1.6b-en_fr),
   acceptez les conditions si demandé, créez un token HF (*Settings → Access Tokens*) et
   mettez `HF_TOKEN=hf_...` dans le `.env`.

## Déploiement (la commande)
```bash
./deploy/deploy_modal.sh
# équivaut à : modal deploy deploy/modal_app.py
```
La première construction d'image prend quelques minutes (torch CUDA + `moshi`). Modal
affiche ensuite l'**URL publique**, du type :
`https://<vous>--moshi-voice-assistant-voiceassistant-web.modal.run`

## Brancher Twilio
```bash
python3 scripts/twilio_setup_number.py --webhook https://VOTRE-URL.modal.run/twilio/webhook
```
La WebSocket est déduite automatiquement (`wss://VOTRE-URL.modal.run/ws/voice`). Si un
appel n'a pas de son, fixez explicitement dans le `.env` puis redéployez :
`PUBLIC_WS_URL=wss://VOTRE-URL.modal.run/ws/voice`.

## Suivre / gérer
```bash
modal app logs moshi-voice-assistant       # logs en direct (voir « Kyutai 1.6B : ... temps réel »)
modal app stop moshi-voice-assistant       # tout arrêter
```

## Coût : chaud vs scale-to-zero
- Par défaut **`min_containers=1`** : une box GPU reste chaude → **aucun cold start**
  pendant un appel, mais vous payez le GPU tant qu'elle est chaude.
- Pour **couper la nuit** (scale-to-zero total) :
  ```bash
  MODAL_MIN_CONTAINERS=0 ./deploy/deploy_modal.sh
  ```
  ⚠️ Le **premier appel** après une période d'inactivité subira alors le chargement du
  modèle (~10-40 s) → Twilio risque de raccrocher. Idéal : garder chaud aux heures
  d'ouverture, couper la nuit (planifiable côté Modal).

## Choisir le GPU
Défaut A10G (24 Go, confortable). Alternatives au déploiement :
```bash
MODAL_GPU=L4 ./deploy/deploy_modal.sh      # moins cher
MODAL_GPU=A100 ./deploy/deploy_modal.sh    # plus rapide
```

## Voix française
Le 1.6B est **en + fr**. La voix par défaut est un timbre du corpus *expresso*
(accent possible). Pour une voix française native, choisissez un fichier du dépôt
[kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices) et mettez dans le `.env` :
```
KYUTAI_TTS_VOICE=<chemin/dans/le/dépôt/voix.wav>
```

## Attendu
Sur GPU, le 1.6B tourne **≥ temps réel** (~220 ms de latence) → voix fluide, sans
saccade, la vraie voix d'unmute.sh. Vérifiez dans les logs :
`Kyutai 1.6B : … (xN.NN temps réel)` avec un facteur **> 1**.

> ⚠️ Non validé en conditions réelles depuis cet environnement (pas de GPU/compte Modal
> ici). Le service TTS suit fidèlement l'API PyTorch officielle de Kyutai
> (`scripts/tts_pytorch_streaming.py`). Au premier déploiement, surveillez les logs :
> si l'API `script_to_entries` / `TTSGen` diffère de la version installée de `moshi`,
> l'ajustement est localisé dans `api/app/voice/kyutai_tts.py`.
