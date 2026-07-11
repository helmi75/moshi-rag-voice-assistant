# Architecture

## Vue d'ensemble (phase 1, actuelle)

```
Appel entrant
     │
     ▼
┌─────────┐  webhook HTTP (To, CallSid, SpeechResult)   ┌──────────────────────────┐
│ Twilio  │ ───────────────────────────────────────────▶│ FastAPI (api/app/main.py)│
│ STT/TTS │ ◀─────────────────────────────────────────── │  ├─ tenants.py  (routage │
└─────────┘        TwiML <Say> + <Gather>               │  │   par numéro appelé)  │
                                                        │  ├─ llm.py (Claude +     │
                                                        │  │   outils métier)      │
                                                        │  └─ reservations.py      │
                                                        └──────────┬───────────────┘
                                                                   │
                                                        ┌──────────▼───────────┐
                                                        │ SQLite (data/app.db) │
                                                        │ tenants, réservations│
                                                        └──────────────────────┘
```

- **Un déploiement, N clients** : le numéro Twilio appelé (`To`) identifie le tenant.
  Chaque tenant a sa base de connaissances, sa langue, son message d'accueil.
- **Le LLM (Claude, API Anthropic)** reçoit la KB du tenant en prompt système et expose
  des outils métier (function calling) : `check_availability`, `create_reservation`.
  La boucle d'outils est dans `api/app/llm.py`.
- **La voix (phase 1)** est déléguée à Twilio : `<Gather input="speech">` pour le STT,
  `<Say>` pour le TTS. Simple et sans infrastructure, au prix d'une latence de 2-4 s.
  La phase 2 (voir ROADMAP.md) remplace ce transport par Twilio Media Streams + Pipecat
  sans toucher au cerveau.

## Choix techniques et justifications

### Pourquoi Moshi a été retiré

Le projet a démarré sur Moshi (Kyutai), un modèle speech-to-speech full-duplex. Retiré
pour trois raisons :

1. **Incontrôlable pour un assistant métier** : pas de function calling fiable, pas de
   moyen robuste de le contraindre aux informations du commerce (horaires, menu) — or
   c'est précisément le produit.
2. **Coût fixe** : GPU 24 Go obligatoire 24h/24 (~200-400 €/mois) même sans un seul appel.
3. **Intégration téléphonique complexe** : Moshi attend un flux audio full-duplex ;
   le brancher sur Twilio aurait demandé tout le travail de la phase 2 sans les
   bénéfices de contrôle du pipeline STT→LLM→TTS.

L'historique reste dans git (`git log -- moshi/`).

### Pourquoi pas de RAG vectoriel (pour l'instant)

La base de connaissances d'un restaurant ou d'un cabinet tient en 1 à 5 K tokens : elle
est injectée intégralement dans le prompt système (`llm.build_system_prompt`). C'est plus
simple, plus fiable (pas de rappel manqué) et moins cher qu'un vector store, et le prompt
système bénéficie du prompt caching d'Anthropic.

**Chemin d'upgrade** (phase 4) : quand un tenant aura des documents volumineux (menus PDF,
sites web), on ajoutera une ingestion → chunking → embeddings → vector store (pgvector),
et `build_system_prompt` injectera les passages récupérés au lieu de la KB brute.
L'interface ne change pas.

### Stockage

SQLite (`data/app.db`, stdlib `sqlite3`) : zéro dépendance, suffisant pour la phase pilote.
Migration prévue vers PostgreSQL en phase 3 (les requêtes sont volontairement basiques).
La mémoire de conversation par appel est en RAM (dict par `CallSid`, TTL 1 h) → Redis
quand l'API sera répliquée.

### Modèle LLM

`claude-sonnet-5` par défaut (variable `LLM_MODEL`), avec `output_config.effort: "low"` :
les tours de parole téléphoniques sont courts et la latence prime. Monter en gamme
(`claude-opus-4-8`) se fait par variable d'environnement, par tenant plus tard.

## Phase 2 — transport streaming (implémenté, `VOICE_MODE=stream`)

La boucle Gather/Say (2-4 s de latence) est doublée d'un pipeline audio streaming
(`api/app/voice/bot.py`). Stack arrêtée après étude comparative (détail et sources :
[docs/VOICE_STACK.md](docs/VOICE_STACK.md)) :

```
Appel ──▶ POST /twilio/voice ──▶ TwiML <Connect><Stream>   [VOICE_MODE=stream]
Appel ──▶ WS /ws/voice (Twilio Media Streams)
              │  (résolution du tenant via <Parameter To>)
              ▼
        ┌──────────────────── Pipecat ────────────────────┐
        │  STT Deepgram fr (phase A) → Kyutai (phase B)   │
        │        │   VAD Silero + smart-turn v3 (barge-in)│
        │        ▼                                        │
        │  Claude + outils métier (mêmes TOOLS,           │
        │  même prompt système que le mode gather)        │
        │        │                                        │
        │        ▼                                        │
        │  TTS Cartesia fr (phase A) → Kyutai (phase B)   │
        └─────────────────────────────────────────────────┘
                       Latence cible ≈ 1,0-1,3 s
```

Règle d'architecture : **le cerveau (`llm.py`, `tenants.py`, `reservations.py`) ignore
le transport**. Gather/Say aujourd'hui, Media Streams/Pipecat demain, 100 % local
(Kyutai + Qwen3 8B quantisé sur une RTX 4090) après-demain — mêmes modules, seuls les
étages audio de Pipecat changent. Chaque étage est interchangeable API ↔ auto-hébergé.

## Sécurité / production (à traiter avant mise en production réelle)

- Valider la signature des webhooks Twilio (`X-Twilio-Signature`)
- HTTPS via Caddy (décommenter le bloc domaine dans `caddy/Caddyfile`)
- Secrets hors du repo (`.env` non versionné)
- Limitation de débit sur les webhooks
