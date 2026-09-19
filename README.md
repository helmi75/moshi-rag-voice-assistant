# Helmane — l'assistante téléphonique des restaurants

Une assistante vocale qui décroche à la place de l'équipe, en salle : elle prend les
réservations (et les modifie ou les annule au téléphone), répond aux questions pratiques
à partir de la fiche de l'établissement, et prend un message quand elle ne peut pas
traiter la demande. Le restaurateur est prévenu par e-mail et retrouve tout dans l'admin.

**Un déploiement, plusieurs établissements** : chacun est identifié par son numéro
Twilio, avec sa voix, son accueil, sa base de connaissances, ses horaires et ses comptes.

## Le chemin d'un appel

Il n'y en a qu'un depuis le 18/09/2026 (l'ancienne boucle Gather/Say et les moteurs
locaux restent lisibles au tag `archive/moteurs-locaux`) :

```
Appel ─▶ Twilio ─▶ POST /twilio/voice (signé) ─▶ TwiML <Connect><Stream>
       ─▶ WS /ws/voice (μ-law 8 kHz, poignée de main signée) ─▶ Pipecat
            ├─ Deepgram nova-3 : transcription (décroche en multilingue, se fixe sur une langue)
            ├─ LLM via OpenRouter : conversation + outils (disponibilité, réservation,
            │    modification, annulation, message) — refus côté serveur hors horaires
            └─ moshi-server sur Modal (GPU L4) : voix Moshi 1.6B, clé privée
       ◀─ audio vers l'appelant (accueil pré-rendu, musique d'attente pendant un réveil GPU)
```

Détail des choix : [ARCHITECTURE.md](ARCHITECTURE.md).

## Ce que fait le produit

| Fonction | Où |
|---|---|
| Réservation, modification, annulation au téléphone (par le numéro qui appelle) | `app/llm.py`, `app/reservations.py` |
| Horaires d'ouverture appliqués par le serveur, fermetures exceptionnelles | `app/disponibilite.py`, admin « Horaires d'ouverture » |
| Nom du dernier passage proposé au lieu d'être redemandé | `reservations.dernier_nom` |
| Messages pris pour l'équipe, rappels à faire | `app/messages.py` |
| E-mail au restaurateur à chaque réservation, modification, annulation, message | `app/notifications.py` (SMTP) |
| Admin : parc, salle de contrôle, journal des appels (transcription, écoute), réservations, voix, fiche, comptes | `app/admin/` — `/admin` |
| Supervision : 14 contrôles, sonde `/supervision`, alerte GitHub Actions | `app/supervision.py` |
| RGPD : purges automatiques, droit à l'effacement | `app/rgpd.py`, [docs/RGPD.md](docs/RGPD.md) |
| Formules et plafond mensuel (compte, prévient, ne coupe jamais la ligne) | `app/plans.py`, `app/quotas.py` |

## Démarrer

Prérequis : Docker ; des clés OpenRouter, Deepgram et Twilio ; moshi-server déployé sur
Modal ([docs/MODAL.md](docs/MODAL.md)).

```bash
cp env.example .env      # OPENROUTER_API_KEY, DEEPGRAM_API_KEY, TWILIO_*, MOSHI_TTS_URL,
                         # MOSHI_TTS_API_KEY, PUBLIC_WS_URL, ADMIN_EMAIL, ADMIN_PASSWORD…
docker compose up -d --build
curl -s localhost:8000/health
```

Dans une base vide, un restaurant de démonstration est semé sur le numéro `TWILIO_NUMBER`
(`SEED_DEMO=0` pour ne rien semer). Ensuite, les établissements se gèrent dans `/admin`.
Pour recevoir de vrais appels, Twilio doit joindre l'application en HTTPS : voir
[docs/TWILIO_SETUP.md](docs/TWILIO_SETUP.md).

**Production** : VPS + Caddy + `scripts/deploy.sh`, tout est dans
[docs/DEPLOY.md](docs/DEPLOY.md).

## Tests

```bash
cd api
pip install -r app/requirements.lock -r tests/requirements-test.txt
python -m pytest tests -q                  # aucun réseau, tout est mocké
cd .. && python3 scripts/mutation_check.py # chaque garde-fou critique doit faire rougir un test
```

La CI (`.github/workflows/ci.yml`) joue aussi `ruff check api/app` et `pip-audit` sur le
verrou. Un appel réel se vérifie avec [docs/RECETTE.md](docs/RECETTE.md).

## Structure

```
api/
  app/
    main.py              webhooks Twilio, flux média, sondes /health et /supervision
    llm.py               prompt système + outils métier (partagés voix et SMS)
    voice/               pipeline Pipecat, voix moshi-server, accueil, langue, journal
    admin/               plateforme admin (Jinja2 + htmx, rendue côté serveur)
    tenants.py reservations.py messages.py calls.py users.py   données (SQLite)
    disponibilite.py horloge.py notifications.py twilio_signature.py
    supervision.py rgpd.py quotas.py plans.py taches.py db.py
    requirements.txt     l'intention ; requirements.lock : les versions exactes installées
  tests/
deploy/modal_moshi_server.py   le serveur de voix sur Modal
scripts/                       déploiement, sauvegarde, bancs d'essai, contrôle de mutation
caddy/Caddyfile                TLS, en-têtes de sécurité (CSP)
docs/                          déploiement, recette, supervision, RGPD, tarifs, passation
```

## Variables essentielles

La liste complète, commentée, est dans [env.example](env.example). Une variable posée
dans `.env` n'atteint le conteneur que si `docker-compose.yml` la liste.

| Variable | Rôle |
|---|---|
| `OPENROUTER_API_KEY`, `LLM_MODEL` | LLM (production : `google/gemini-2.5-flash`) |
| `DEEPGRAM_API_KEY` | transcription |
| `MOSHI_TTS_URL`, `MOSHI_TTS_API_KEY` | serveur de voix et sa clé privée (même valeur qu'au `modal deploy`) |
| `PUBLIC_WS_URL`, `PUBLIC_URL` | URL publiques : flux média, et base de la signature Twilio |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SIGNATURE` | compte Twilio ; vérification des signatures (`enforce` par défaut) |
| `TWILIO_NUMBER`, `SEED_DEMO` | restaurant de démonstration d'une base vide |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `SESSION_SECRET` | premier super-admin, sessions |
| `SUPERVISION_TOKEN` | jeton de la sonde (en-tête `X-Supervision-Token`) |
| `SMTP_HOST`, `SMTP_FROM`, `SMTP_*` | e-mails au restaurateur |

## Documentation

| Document | Pour |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | les choix techniques, et pourquoi |
| [docs/DEPLOY.md](docs/DEPLOY.md) | mettre en production, sauvegarder, restaurer |
| [docs/MODAL.md](docs/MODAL.md) | le serveur de voix et sa clé |
| [docs/TWILIO_SETUP.md](docs/TWILIO_SETUP.md) | brancher un numéro |
| [docs/SUPERVISION.md](docs/SUPERVISION.md) | les contrôles et l'alerte |
| [docs/RECETTE.md](docs/RECETTE.md) | ce qu'on vérifie sur un vrai appel |
| [docs/RGPD.md](docs/RGPD.md) · [docs/TARIFS.md](docs/TARIFS.md) | données personnelles · grille tarifaire |
| [docs/PASSATION.md](docs/PASSATION.md) | l'état de la production et ses accès |
| [ROADMAP.md](ROADMAP.md) · [CHANGELOG.md](CHANGELOG.md) | la suite · l'historique |
