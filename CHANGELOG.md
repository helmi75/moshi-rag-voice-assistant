# Changelog

## v0.2.0 — 2026-07-14 — Pipeline vocal temps réel + voix française fiable

Point stable pour un premier client. Ce qui marche de bout en bout :

- **Routage multi-tenant** par numéro Twilio appelé, avec réalignement automatique du
  tenant de démo sur `TWILIO_NUMBER` au démarrage (fin du « ce numéro n'est pas encore
  configuré » quand un mauvais numéro s'était figé dans le volume Docker).
- **Mode `gather` (défaut, recommandé sur CPU)** : voix **neuronale** Amazon Polly
  française (Léa) via `<Say voice="Polly.Lea-Neural">` — naturelle, incluse dans Twilio,
  zéro latence, zéro clé. Surchargeable par `TWILIO_VOICE`.
- **Mode `stream`** : pipeline Pipecat temps réel (Deepgram STT + LLM OpenRouter +
  Pocket TTS / Cartesia). Voix Kyutai « Pocket TTS » branchée.
- **LLM** OpenRouter avec function calling (`check_availability`, `create_reservation`).

**Limite connue, cause de la voix saccadée en `stream` :** Pocket TTS français
(`french_24l`) sur **CPU** met 5–10 s à produire son premier morceau, au-delà du seuil
de Pipecat → l'audio est haché ou perdu. Sur CPU, utiliser `VOICE_MODE=gather` (Polly)
ou `TTS_PROVIDER=cartesia`. Le support **GPU** (qui rend Pocket TTS temps réel) est la
suite, sur une branche dédiée.

---

## 2026-07 — Voix Kyutai (Pocket TTS) dans le pipeline streaming

### Pourquoi
La voix du mode `gather` est le TTS robotique de Twilio. L'utilisateur veut la voix
de la famille Unmute (Kyutai). Kyutai TTS 1.6B (la voix exacte d'unmute.sh) exige un
GPU ; **Pocket TTS** (kyutai-labs, MIT, 100 M params) offre la même famille de voix
en tournant sur **CPU** — dont la voix `estelle`, littéralement un échantillon du
site Unmute.

### Ajouté
- `api/app/voice/pocket_tts.py` : `PocketTTSService(TTSService)` — service TTS Pipecat
  basé sur Pocket TTS. Chargement du modèle + voix en singleton (une fois par process),
  génération streaming (`generate_audio_stream`) offloadée en thread, resample vers le
  8 kHz Twilio, sérialisée par un lock (modèle non thread-safe). Voix préréglée
  (`estelle`, défaut) ou clonage depuis un extrait audio (`POCKET_TTS_VOICE=chemin/url`).
- `bot.build_tts()` : sélecteur `TTS_PROVIDER` — **`pocket` par défaut** (voix Kyutai,
  CPU, sans clé), `cartesia` en alternative API.
- `pocket-tts[audio]` dans requirements ; `libsndfile1` dans le Dockerfile (soundfile) ;
  `TTS_PROVIDER`/`POCKET_TTS_VOICE`/`POCKET_TTS_LANGUAGE`/`HF_HOME` dans compose et env.
- 9 nouveaux tests (`api/tests/test_pocket_tts.py`, modèle mocké) : résolution de voix,
  frames audio produites, gestion d'erreur, singleton, sélecteur `build_tts`. 45 tests
  au total, toujours zéro appel réseau.

### Notes
- Le français impose la variante `french_24l` (bug attrapé en test réel : `french`
  seul lève une ValueError côté pocket-tts).
- Limite assumée phase A : génération TTS sérialisée (faible simultanéité). Le 1.6B GPU
  reste la cible phase B pour la qualité maximale et la concurrence.

---

## 2026-07 — Bascule du LLM vers OpenRouter (choix libre du modèle)

### Pourquoi
Blocage de facturation côté compte Anthropic (compte classé « Team », questionnaire
Trust & Safety requis avant tout achat de crédits). OpenRouter donne accès à
n'importe quel modèle (Claude, GPT, Gemini, Llama, Mistral, DeepSeek...) avec une
seule clé, un onboarding paiement plus simple, et surtout des **modèles gratuits
avec function calling** (`openrouter/free`) — débloque les tests immédiatement sans
dépenser, tout en donnant la liberté de choix de modèle.

### Modifié
- `api/app/llm.py` : `anthropic` → `openai` (`AsyncOpenAI` pointé sur
  `https://openrouter.ai/api/v1`), boucle d'outils réécrite au format Chat
  Completions (function calling OpenAI : `tool_calls`, arguments JSON en chaîne).
  `MODEL` par défaut : `openrouter/free`. Invariant conservé : l'historique retourné
  ne contient jamais le message système (ré-injecté à chaque appel) — **aucun
  changement dans `main.py`**
- `api/app/voice/bot.py` : `AnthropicLLMService` → `OpenAILLMService` (pipecat,
  pointé sur OpenRouter) — seul le bloc de construction du service change, le reste
  du pipeline (contexte, VAD, function calling) était déjà écrit de façon neutre
- `api/app/requirements.txt` : `anthropic` → `openai`, extra pipecat
  `[anthropic,...]` → `[openai,...]`
- `env.example`, `docker-compose.yml`, `api/tests/conftest.py` : `ANTHROPIC_API_KEY`
  → `OPENROUTER_API_KEY` (+ `OPENROUTER_BASE_URL`, `OPENROUTER_SITE_URL`,
  `OPENROUTER_APP_NAME` optionnels)
- Tests : `TestLLMToolLoop` réécrite au format OpenAI (2 nouveaux tests : schéma
  d'outils, robustesse aux arguments JSON malformés du modèle) — 36 tests au total,
  toujours zéro appel réseau. `test_voice_stream.py` inchangé (déjà neutre vis-à-vis
  du fournisseur LLM)

---

## 2026-07 — Phase 2A : voix temps réel (Twilio Media Streams + Pipecat)

### Ajouté
- **Mode `VOICE_MODE=stream`** : pipeline audio streaming, latence ~1 s, barge-in
  - `api/app/voice/bot.py` : pipeline Pipecat 1.5 (Deepgram STT fr → Claude + outils
    → Cartesia TTS fr), VAD Silero + smart-turn v3, raccrochage auto si identifiants
    Twilio présents
  - `POST /twilio/voice` renvoie `<Connect><Stream>` en mode stream (tenant transmis
    via `<Parameter To>`)
  - `WS /ws/voice` : poignée de main Media Streams, résolution du tenant, garde-fous
    (tenant inconnu → 1008, protocole invalide → 1002, crash pipeline → 1011)
- Cerveau partagé entre les deux modes : `llm.run_tool` (ex-`_run_tool`), mêmes
  `TOOLS` et prompt système
- 17 nouveaux tests (`api/tests/test_voice_stream.py`) : TwiML stream, WebSocket,
  outils partagés, pont Pipecat — LLM et bot mockés, zéro réseau
- `/health` expose `voice_mode` ; `tests/test_e2e.sh` s'adapte au mode du serveur
- Docker : `pipecat-ai[anthropic,cartesia,deepgram,silero]~=1.5.0`, `libgomp1`,
  variables `VOICE_MODE`/`PUBLIC_WS_URL`/`DEEPGRAM_*`/`CARTESIA_*` dans compose et env

### Inchangé
- Mode `gather` par défaut : fonctionne sans clés Deepgram/Cartesia, aucun test cassé

---

## 2026-07 — Pivot SaaS : cerveau multi-tenant, abandon de Moshi (phase 1)

Refonte complète orientée produit SaaS (voir ROADMAP.md et ARCHITECTURE.md).

### Ajouté
- **Multi-tenant** : `api/app/tenants.py` — routage par numéro Twilio appelé (`To`),
  base de connaissances, langue et message d'accueil par commerce (SQLite)
- **LLM Claude** : `api/app/llm.py` — API Anthropic avec function calling
  (`check_availability`, `create_reservation`), KB du tenant en prompt système,
  mémoire de conversation par appel (`CallSid`)
- **Réservations SQLite** : `api/app/reservations.py` réécrit (fichier JSON → SQLite,
  rattachement au tenant), endpoint `GET /tenants/{id}/reservations`
- `ROADMAP.md` (phases 1 → 4) et `ARCHITECTURE.md`
- Suite de tests réécrite : 17 tests, LLM mocké (`api/tests/`)

### Supprimé
- Service **Moshi** (`moshi/`, `MOSHI_INTEGRATION.md`) et la réservation GPU du
  docker-compose : modèle speech-to-speech incontrôlable pour un assistant métier et
  coût GPU fixe — remplacé par des APIs cloud (justification dans ARCHITECTURE.md)
- `api/app/rag.py` (stub) : la KB tenant tient dans le prompt système ; un vrai RAG
  vectoriel est planifié en phase 4 quand les KB grossiront

### Modifié
- `api/app/main.py` : webhooks Twilio branchés sur le LLM (l'appel placeholder Moshi
  qui ne fonctionnait pas est supprimé), gestion d'erreur polie, échappement XML
- `docker-compose.yml` : 2 services (api + caddy), plus de GPU, volume de données
- `env.example` : `ANTHROPIC_API_KEY` + `LLM_MODEL` remplacent les variables Moshi
- `README.md` : pitch SaaS, quickstart sans GPU
- `tests/test_e2e.sh` : adapté au routage multi-tenant

---

## Historique — Nettoyage et optimisation (prototype Moshi)

- Suppression de `README.txt` obsolète, imports inutilisés nettoyés
- Refactorisation de `main.py` (fonctions `_call_moshi_api`, `_process_user_message`)
- Voir `git log` pour le détail du prototype initial basé sur Moshi/Vast.ai
