# Assistant téléphonique IA pour commerces (SaaS)

Un assistant qui répond au téléphone à la place des commerces débordés d'appels —
restaurants, cabinets médicaux, artisans : renseigner les clients (horaires, menu,
adresse), prendre des réservations, 24h/24.

**Multi-tenant dès le départ** : un seul déploiement sert plusieurs commerces, chacun
identifié par son numéro de téléphone Twilio, avec sa propre base de connaissances et
ses réservations.

📍 Voir [ROADMAP.md](ROADMAP.md) pour le plan produit et [ARCHITECTURE.md](ARCHITECTURE.md)
pour les choix techniques (dont l'abandon de Moshi/GPU).

## 🚀 Fonctionnement

Deux modes vocaux, même cerveau (`VOICE_MODE`) :

**Mode `gather` (défaut — zéro clé supplémentaire, latence 2-4 s)**
```
Appel → Twilio (STT) → FastAPI → tenant (par numéro appelé) → Claude + outils métier
      ← Twilio (TTS) ←         ← réponse + réservation en base
```

**Mode `stream` (temps réel — latence ~1 s, barge-in)**
```
Appel → Twilio Media Streams (WebSocket audio) → Pipecat
        → STT Deepgram (fr) → Claude + outils → TTS Cartesia (fr) → audio
```

- **FastAPI** : webhooks Twilio voix/SMS, WebSocket Media Streams, routage multi-tenant
- **Pipecat** : orchestration temps réel (VAD Silero + smart-turn, interruptions)
- **Claude (API Anthropic)** : conversation + function calling (`create_reservation`,
  `check_availability`), base de connaissances du commerce en prompt système
- **SQLite** : tenants et réservations (`data/app.db`)
- **Caddy** : reverse proxy TLS (WebSockets inclus)
- **Aucun GPU requis** : tout fonctionne sur un petit VPS

## 📋 Prérequis

- Docker et Docker Compose (ou Python 3.11+ en local)
- Une clé API Anthropic ([platform.claude.com](https://platform.claude.com))
- Un compte Twilio avec un numéro de téléphone

## 🛠️ Installation

### 1. Cloner et configurer

```bash
git clone https://github.com/helmi75/moshi-rag-voice-assistant.git
cd moshi-rag-voice-assistant
cp env.example .env
# Éditez .env : ANTHROPIC_API_KEY + identifiants Twilio
```

### 2. Démarrer

```bash
docker compose up -d --build
docker compose logs -f api
```

Ou en local sans Docker :

```bash
cd api
pip install -r app/requirements.txt
uvicorn app.main:app --reload
```

Au premier démarrage, un restaurant de démonstration est créé, rattaché au numéro
`TWILIO_NUMBER` de votre `.env`.

### 3. Connecter Twilio

Pointez le webhook vocal de votre numéro Twilio sur `https://VOTRE_DOMAINE/twilio/webhook`
(guide : [TWILIO_SETUP.md](TWILIO_SETUP.md), ou script automatique `python setup_twilio.py`).

Appelez votre numéro : l'assistant décroche, renseigne et prend des réservations.

### 4. (Optionnel) Activer la voix temps réel

Dans `.env` :

```bash
VOICE_MODE=stream
PUBLIC_WS_URL=wss://VOTRE_DOMAINE/ws/voice
DEEPGRAM_API_KEY=...      # STT français streaming (deepgram.com)
CARTESIA_API_KEY=...      # TTS français streaming (cartesia.ai)
CARTESIA_VOICE_ID=...     # une voix française du catalogue Cartesia
```

Puis `docker compose up -d --build`. La latence passe de 2-4 s à ~1 s et l'appelant
peut couper la parole à l'assistant. Repasser à `VOICE_MODE=gather` ramène au mode
sans clés. La bascule vers Kyutai STT/TTS auto-hébergés (100 % local) est prévue en
phase B — voir [docs/VOICE_STACK.md](docs/VOICE_STACK.md).

## 🧪 Tests

```bash
# Tests unitaires (LLM mocké, aucun appel réseau)
cd api && pip install -r tests/requirements-test.txt && pytest tests/ -v

# Test de bout en bout contre une instance qui tourne
./tests/test_e2e.sh localhost +33100000000

# Simuler un tour de conversation à la main
curl -X POST http://localhost:8000/twilio/voice \
  --data-urlencode "CallSid=CA_test" \
  --data-urlencode "To=+33100000000" \
  --data-urlencode "SpeechResult=Je voudrais réserver pour 4 personnes demain à 20h, au nom de Durand"

# Voir les réservations du tenant 1
curl http://localhost:8000/tenants/1/reservations
```

## 📁 Structure du projet

```
├── api/
│   ├── app/
│   │   ├── main.py          # Webhooks Twilio (voix, SMS), routage tenant
│   │   ├── llm.py           # Claude + outils métier (function calling)
│   │   ├── tenants.py       # Tenants et résolution par numéro appelé
│   │   ├── reservations.py  # Réservations (SQLite)
│   │   └── db.py            # Connexion et schéma SQLite
│   ├── tests/               # Tests pytest (LLM mocké)
│   └── Dockerfile
├── caddy/Caddyfile          # Reverse proxy
├── docker-compose.yml
├── ROADMAP.md               # Plan produit (phases 1 → 4)
├── ARCHITECTURE.md          # Choix techniques
└── env.example
```

## 🔧 Variables d'environnement

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Clé API Anthropic (obligatoire) |
| `LLM_MODEL` | Modèle Claude (défaut : `claude-sonnet-5`) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Identifiants Twilio |
| `TWILIO_NUMBER` | Numéro du tenant de démo (E.164) |
| `DB_PATH` | Chemin SQLite (défaut : `./data/app.db`) |

## ➕ Ajouter un commerce (tenant)

Pour l'instant directement en base (le dashboard arrive en phase 3, voir ROADMAP.md) :

```bash
sqlite3 data/app.db "INSERT INTO tenants (name, business_type, phone_number, language, greeting, knowledge_base)
VALUES ('Pizzeria Bella', 'restaurant', '+33187654321', 'fr-FR',
        'Bonjour, Pizzeria Bella, que puis-je faire pour vous ?',
        '## Horaires\nOuvert du mardi au dimanche, 12h-14h30 et 19h-23h. ...');"
```

Chaque numéro Twilio supplémentaire pointe vers le même webhook : le routage se fait
automatiquement par le champ `To`.

## 📄 Licence

Voir le fichier LICENSE.

## 👤 Auteur

**helmi75**
