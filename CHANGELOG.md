# Changelog

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
