# Architecture

Ce document dit **pourquoi** le système est construit ainsi. Le « comment » est dans le
code, dont les commentaires portent la raison de chaque garde-fou.

## Vue d'ensemble

```
                 ┌────────────────────────── VPS (Docker) ──────────────────────────┐
Appel ─▶ Twilio ─┼─▶ Caddy (TLS, CSP) ─▶ FastAPI  api/app/main.py                    │
                 │                        ├─ /twilio/voice|sms|webhook  (signés)     │
                 │                        ├─ /ws/voice ─▶ Pipecat  api/app/voice/    │
                 │                        │     Deepgram ─▶ LLM (OpenRouter) ─▶ voix │──▶ Modal (GPU L4)
                 │                        ├─ /admin      (Jinja2 + htmx)             │    moshi-server
                 │                        └─ /health, /supervision                   │
                 │                     SQLite (volume api_data) · enregistrements    │
                 └───────────────────────────────────────────────────────────────────┘
                   GitHub Actions ─▶ /supervision toutes les 15 min (alerte si 503)
```

- **Un déploiement, N établissements** : le numéro appelé (`To`) désigne l'établissement
  (`tenants.get_by_phone`). Chacun a sa base de connaissances, ses horaires, sa voix, son
  accueil, ses comptes et sa formule.
- **Un seul chemin d'appel** (depuis le 18/09/2026) : Twilio Media Streams + Pipecat. La
  boucle `<Gather>`/`<Say>` et les moteurs locaux (Pocket, Kyutai en PyTorch, Cartesia)
  ont été retirés : deux chemins, c'était deux produits à tester, et le second n'était
  plus ni journalisé ni compté au forfait. Code lisible au tag `archive/moteurs-locaux`.

## Le chemin d'un appel

1. `POST /twilio/voice` — signature Twilio vérifiée, établissement résolu, TwiML
   `<Connect><Stream>` avec le numéro appelant en paramètre.
2. `WS /ws/voice` — signature de la poignée de main vérifiée **avant** `accept()` ; le
   numéro appelant passe par `calls.numero_appelant` (un appel masqué n'a pas de numéro),
   l'appel est ouvert en base, puis `voice/bot.run_bot`.
3. Pipecat : VAD Silero + smart-turn (modèle partagé entre les appels) ; **Deepgram
   nova-3** décroche en `multi` puis SUIT la langue de l'appel (`voice/langue.py`) — il
   se fixe sur celle de l'établissement quand elle est avérée, et redevient bilingue dès
   qu'on constate qu'on n'entend plus l'appelant ; **LLM** via OpenRouter, raisonnement
   coupé (c'est du silence au téléphone) ; **voix** servie par `moshi-server` sur Modal,
   clé privée (`voice/moshi_server_tts.py`).
4. L'accueil est un WAV pré-rendu par établissement : l'appelant l'entend tout de suite,
   même quand le GPU se réveille ; la musique d'attente meuble le réveil. Il est
   pré-inscrit au contexte du modèle **parce qu'il ne traverse pas le pipeline** ; tout
   ce que dit le TTS du pipeline, l'agrégateur l'inscrit seul (`bot.amorce_assistante`).
5. À la fin : durée, transcription, latences par tour et journal de bord en base ;
   enregistrement deux pistes si activé (`voice/enregistrement.py`) ; puis, hors du
   chemin d'appel, une phrase de résumé (`resume.py`) qui remplace l'extrait brut dans
   la liste des appels.

**Règle d'architecture** : le cerveau (`llm.py`, `reservations.py`, `messages.py`)
ignore le transport. `llm.run_tool` est le seul point où un outil touche aux données,
pour la voix comme pour le SMS.

## Les outils et ce que le serveur refuse

Le modèle propose, le serveur dispose. `llm.run_tool` refuse — avec un message que le
modèle relit, jamais une exception — un créneau passé, illisible ou **hors des horaires
d'ouverture** (`disponibilite.py` ; horaires non renseignés = aucun refus, et l'admin le
signale). La modification et l'annulation ne touchent que les réservations **du numéro
qui appelle**, jamais celles qu'un modèle aurait devinées.

Après chaque écriture réussie, `notifications.planifier` envoie l'e-mail au restaurateur
en tâche de fond : le résultat de l'outil revient au modèle sans attendre le SMTP.

Le nom de la dernière réservation du numéro est **proposé** dans le prompt
(`reservations.dernier_nom`), jamais présumé, et seulement s'il ressemble à un nom.

## Données

SQLite (`data/app.db`, volume `api_data`), migrations numérotées par `PRAGMA
user_version` (`db._MIGRATIONS` ; une migration livrée ne se réécrit jamais).

| Table | Contenu |
|---|---|
| `tenants` | établissements : numéro, fiche, accueil, voix, formule, horaires (JSON), e-mail de notification |
| `reservations` | réservations ; une annulée **reste**, horodatée (`cancelled_at`) |
| `calls` | journal des appels : durée, transcription, latences, journal de bord |
| `messages` | messages pris pour l'équipe, à rappeler |
| `users` | super-admin et restaurateurs (bcrypt) |
| `supervision` | ardoise des mesures (relève Twilio, purge, sonde d'écriture) |

- **Hors de la boucle d'événements** : la boucle porte l'audio de tous les appels en
  cours. Tout accès SQLite du chemin d'appel et de l'admin passe par un fil
  (`db.hors_boucle`, ou un gestionnaire `def` que FastAPI exécute dans son pool). WAL,
  `synchronous=NORMAL`.
- **Heure du restaurant** : stockage en UTC, mais toutes les bornes (« aujourd'hui »,
  « ce mois-ci », « 30 derniers jours ») sont calculées à l'heure de Paris
  (`horloge.py`). Un appel à 00 h 30 le 1er compte dans le bon mois.
- **Tâches de fond** : toutes passent par `taches.lancer` (référence retenue, exception
  journalisée, arrêt propre dans le `lifespan`).

### Pourquoi pas de RAG vectoriel

La fiche d'un restaurant tient en quelques milliers de caractères (plafond admin :
12 000) : elle est injectée entière dans le prompt système. Plus simple, plus fiable (pas
de passage manqué) et moins cher qu'un index vectoriel. Chemin d'upgrade le jour où un
établissement aura des documents volumineux : ingestion → découpage → embeddings, et
`build_system_prompt` injecte les passages retrouvés. L'interface ne change pas.

### Pourquoi Moshi « speech-to-speech » a été abandonné, et pas la voix Moshi

Le projet a démarré sur Moshi full-duplex : pas d'appel d'outil fiable, impossible à
contraindre à la fiche du restaurant — or c'est le produit. On garde la **voix** Moshi
1.6B de Kyutai, servie par `moshi-server` (le serveur de production d'unmute.sh), en
simple étage TTS d'un pipeline STT → LLM → TTS qu'on contrôle.

## Sécurité

| Menace | Parade |
|---|---|
| Requête forgée sur les webhooks ou le flux | `X-Twilio-Signature` vérifiée (`twilio_signature.py`), URL publique reconstruite depuis `PUBLIC_URL` ; mode `log` pour observer avant `enforce` |
| GPU utilisé par un tiers | clé privée `MOSHI_TTS_API_KEY` posée au démarrage du conteneur Modal |
| Admin | session signée, CSRF, bcrypt, limitation des tentatives, cloisonnement par établissement (`deps.resolve_tenant`), CSP stricte (Caddy) |
| Exposition réseau | API liée à `127.0.0.1`, seul Caddy est public ; pare-feu `ufw` |
| Données personnelles | purges automatiques, droit à l'effacement, numéro tronqué dans les journaux |
| Perte de la base | sauvegarde nocturne vérifiée + copie hors du serveur (rclone), surveillées |

## Exploitation

- **Supervision** (`supervision.py`, [docs/SUPERVISION.md](docs/SUPERVISION.md)) : 14
  contrôles, une seule source pour l'écran « Santé & coûts » et la sonde `/supervision`.
  « Pas de mesure » n'est jamais un feu vert.
- **Garde-fous** : chaque protection critique a un test qui rougit quand on la retire
  (`scripts/mutation_check.py`, joué en CI).
- **Dépendances** : `requirements.lock` installé tel quel par l'image et la CI ;
  `ruff` et `pip-audit` en CI.
