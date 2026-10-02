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
            └─ Mistral Voxtral : la voix choisie par l'établissement (30 voix, sans GPU)
       ◀─ audio vers l'appelant (accueil pré-rendu : aucun blanc au décroché)

Assistante en panne ─▶ elle rend la ligne ─▶ POST /twilio/suite ─▶ le restaurant sonne,
                                                                    sinon répondeur
```

Depuis le 28/09/2026, la voix vient de Mistral. La voix Moshi (`moshi-server` sur un GPU
Modal) n'est plus qu'un secours quand la clé Mistral manque ; son retrait est prévu.

Détail des choix : [ARCHITECTURE.md](ARCHITECTURE.md).

## Ce que fait le produit

| Fonction | Où |
|---|---|
| Réservation, modification, annulation au téléphone (par le numéro qui appelle) | `app/llm.py`, `app/reservations.py` |
| Horaires d'ouverture appliqués par le serveur, fermetures exceptionnelles | `app/disponibilite.py`, admin « Ce que l'IA sait » |
| Carnet de réservations : le nôtre, ou celui de resOS (lu et écrit par son API) | `app/connecteurs/`, [docs/RESOS.md](docs/RESOS.md) |
| En cas de panne : renvoi vers le numéro de secours du restaurant, sinon message vocal | `app/renvoi.py`, `app/repondeur.py`, `app/voice/vigie.py` |
| Site vitrine à la racine, et « Rappelez-moi » : l'assistante appelle le numéro laissé | `app/site/`, `app/rappel.py` |
| Nom du dernier passage proposé au lieu d'être redemandé | `reservations.dernier_nom` |
| Messages pris pour l'équipe, rappels à faire | `app/messages.py` |
| E-mail au restaurateur à chaque réservation, modification, annulation, message | `app/notifications.py` (SMTP) |
| Admin : parc, salle de contrôle, journal des appels (transcription, écoute), calendrier des réservations, voix, fiche, comptes ; thème clair ou sombre ; tout à l'heure de Paris | `app/admin/` — `/admin` |
| Coût de chaque appel chiffré poste par poste à sa clôture | `app/calls.py`, admin « Santé & coûts » |
| Supervision : 17 contrôles, sonde `/supervision`, alerte GitHub Actions | `app/supervision.py` |
| RGPD : purges automatiques, droit à l'effacement | `app/rgpd.py`, [docs/RGPD.md](docs/RGPD.md) |
| Formules et plafond mensuel (compte, prévient, ne coupe jamais la ligne) | `app/plans.py`, `app/quotas.py` |

## Démarrer

Prérequis : Docker ; des clés OpenRouter, Deepgram, Twilio et Mistral (voix Voxtral,
[docs/VOXTRAL.md](docs/VOXTRAL.md)).

```bash
cp env.example .env      # OPENROUTER_API_KEY, DEEPGRAM_API_KEY, TWILIO_*, MISTRAL_API_KEY,
                         # PUBLIC_WS_URL, ADMIN_EMAIL, ADMIN_PASSWORD…
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
# Dans l'image Docker (les dépendances verrouillées y sont déjà)
docker run --rm --tmpfs /tmp:exec -v "$PWD:/repo" -w /repo/api moshi-rag-voice-assistant-api \
  sh -c "pip install -q pytest ruff >/dev/null 2>&1; ruff check --config ../ruff.toml app; \
         python -m pytest tests -q -p no:cacheprovider"

# Ou hors Docker (Python 3.12), comme la CI
cd api && pip install -r app/requirements.lock -r tests/requirements-test.txt
python -m pytest tests -q                  # aucun réseau, tout est mocké
```

La CI (`.github/workflows/ci.yml`) joue `ruff check api/app`, `pip-audit` sur le verrou,
la suite, puis `scripts/mutation_check.py` : chaque garde-fou critique est retiré tour à
tour, et un test doit rougir. Un appel réel se vérifie avec
[docs/RECETTE.md](docs/RECETTE.md). Le cycle de travail complet (branche, revue, fusion,
déploiement) est décrit dans [CLAUDE.md](CLAUDE.md).

## Structure

```
api/
  app/
    main.py              webhooks Twilio, flux média, sondes /health et /supervision
    llm.py               prompt système + outils métier (partagés voix et SMS)
    voice/               pipeline Pipecat, voix Mistral (et Moshi en secours), accueil,
                         langue, journal de bord, vigie des pannes
    admin/               plateforme admin (Jinja2 + htmx, rendue côté serveur)
    connecteurs/         carnet de réservations : interne, resOS, faux resOS de test
    tenants.py reservations.py messages.py calls.py users.py   données (SQLite)
    renvoi.py repondeur.py                                     assistante en panne
    site/                page d'accueil publique (prix lus dans plans.py)
    rappel.py            « Rappelez-moi » : l'appel sortant et tous ses plafonds
    disponibilite.py horloge.py notifications.py twilio_signature.py
    supervision.py rgpd.py quotas.py plans.py taches.py db.py
    requirements.txt     l'intention ; requirements.lock : les versions exactes installées
  tests/
deploy/modal_moshi_server.py   la voix de secours Moshi, sur Modal
scripts/                       déploiement, sauvegarde, bancs d'essai, contrôle de mutation
caddy/Caddyfile                TLS, en-têtes de sécurité (CSP)
docs/                          déploiement, recette, supervision, RGPD, tarifs, resOS, voix
```

## Variables essentielles

La liste complète, commentée, est dans [env.example](env.example). Une variable posée
dans `.env` n'atteint le conteneur que si `docker-compose.yml` la liste.

| Variable | Rôle |
|---|---|
| `OPENROUTER_API_KEY`, `LLM_MODEL` | LLM (production : `google/gemini-2.5-flash`) |
| `DEEPGRAM_API_KEY` | transcription |
| `MISTRAL_API_KEY`, `VOIX_PAR_DEFAUT` | voix Mistral (Voxtral) et voix des établissements qui n'ont rien choisi |
| `MOSHI_TTS_URL`, `MOSHI_TTS_API_KEY` | voix de SECOURS Moshi (GPU Modal), seulement si la clé Mistral manque ; abandon prévu |
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
| [docs/VOXTRAL.md](docs/VOXTRAL.md) | les voix de Mistral |
| [docs/RESOS.md](docs/RESOS.md) | le carnet resOS : ce qui est fait, hypothèses et limites |
| [docs/TWILIO_SETUP.md](docs/TWILIO_SETUP.md) | brancher un numéro ; le renvoi en cas de panne |
| [docs/SUPERVISION.md](docs/SUPERVISION.md) | les contrôles et l'alerte |
| [docs/RECETTE.md](docs/RECETTE.md) | ce qu'on vérifie sur un vrai appel |
| [docs/RGPD.md](docs/RGPD.md) · [docs/TARIFS.md](docs/TARIFS.md) | données personnelles · grille tarifaire |
| [CHANGELOG.md](CHANGELOG.md) | ce qui a été livré, et pourquoi |
| [CLAUDE.md](CLAUDE.md) | travailler dans ce dépôt : commandes, cycle de travail, règles que les tests imposent |

Documents historiques, conservés pour leur raisonnement et **non tenus à jour** :
[ROADMAP.md](ROADMAP.md) (état du 19/09/2026 ; le suivi est aujourd'hui dans Jira),
[docs/PASSATION.md](docs/PASSATION.md) (août 2026), [docs/VOICE_STACK.md](docs/VOICE_STACK.md)
(étude de juillet 2026), [docs/MODAL.md](docs/MODAL.md) (la voix de secours Moshi).
