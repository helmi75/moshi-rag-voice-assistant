# Architecture

Ce document dit **pourquoi** le système est construit ainsi. Le « comment » est dans le
code, dont les commentaires portent la raison de chaque garde-fou.

## Vue d'ensemble

```
                 ┌────────────────────────── VPS (Docker) ──────────────────────────┐
Appel ─▶ Twilio ─┼─▶ Caddy (TLS, CSP) ─▶ FastAPI  api/app/main.py                    │
                 │                        ├─ /twilio/voice|sms|webhook  (signés)     │
                 │                        ├─ /ws/voice ─▶ Pipecat  api/app/voice/    │
                 │                        │     Deepgram ─▶ LLM (OpenRouter) ─▶ voix │──▶ Mistral (Voxtral)
                 │                        ├─ /twilio/suite … (assistante en panne)   │    secours : Modal (GPU L4)
                 │                        ├─ /admin      (Jinja2 + htmx)             │
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
   `<Connect><Stream>` avec le numéro appelant en paramètre, suivi d'un
   `<Redirect>/twilio/suite` que Twilio ne lit que si le flux s'arrête sans raccrochage.
2. `WS /ws/voice` — signature de la poignée de main vérifiée **avant** `accept()` ; le
   numéro appelant passe par `calls.numero_appelant` (un appel masqué n'a pas de numéro),
   l'appel est ouvert en base, puis `voice/bot.run_bot`.
3. Pipecat : VAD Silero + smart-turn (modèle partagé entre les appels) ; **Deepgram
   nova-3** décroche en `multi` puis SUIT la langue de l'appel (`voice/langue.py`) — il
   se fixe sur celle de l'établissement quand elle est avérée, et redevient bilingue dès
   qu'on constate qu'on n'entend plus l'appelant. Une voix que le VAD entend sans qu'aucun
   mot n'arrive est retranscrite à part, sinon l'assistante dit « Pardon, je n'ai pas bien
   entendu » (`voice/rattrapage.py`) ; **LLM** via OpenRouter, raisonnement
   coupé (c'est du silence au téléphone) ; **voix** de Mistral (Voxtral), 30 voix au
   choix de l'établissement, en HTTPS et en flux, sans GPU (`voice/voxtral_tts.py`,
   `docs/VOXTRAL.md`). Sans clé Mistral, voix de secours Moshi sur Modal
   (`voice/moshi_server_tts.py`), dont l'abandon est prévu.
4. L'accueil est un WAV pré-rendu par établissement : l'appelant l'entend tout de suite
   (avec la voix de secours Moshi, la musique d'attente meuble le réveil du GPU). Il est
   pré-inscrit au contexte du modèle **parce qu'il ne traverse pas le pipeline** ; tout
   ce que dit le TTS du pipeline, l'agrégateur l'inscrit seul (`bot.amorce_assistante`).
5. À la fin : durée, transcription, latences par tour et journal de bord en base ;
   enregistrement deux pistes si activé (`voice/enregistrement.py`) ; puis, hors du
   chemin d'appel, une phrase de résumé (`resume.py`) qui remplace l'extrait brut dans
   la liste des appels.

6. **Si l'assistante tombe en panne** (`renvoi.py`, ASSISTANTE-118) : un observateur
   (`voice/vigie.py`) compte les erreurs de la voix, du modèle et de la transcription ;
   à la deuxième de suite sur le même maillon — ou si le pipeline s'effondre —
   l'assistante rend la ligne **sans raccrocher**. Twilio lit alors `/twilio/suite` : le
   numéro de secours de l'établissement sonne 15 s pendant ses horaires d'ouverture,
   sinon le client laisse un message (`repondeur.py` le rapatrie avec nos
   enregistrements, puis l'efface de chez Twilio). Pendant trois minutes, les appels
   suivants sont renvoyés dès le décroché. **Aucune commande n'est envoyée à Twilio
   pendant la panne** : c'est lui qui vient chercher la suite, donc le mécanisme tient
   même si notre accès à son API est la panne. Ce que ce mécanisme ne couvre pas : un
   serveur qui ne répond plus du tout (`docs/TWILIO_SETUP.md` §6).

**Règle d'architecture** : le cerveau (`llm.py`, `reservations.py`, `messages.py`)
ignore le transport. `llm.run_tool` est le seul point où un outil touche aux données,
pour la voix comme pour le SMS.

**Le carnet de réservations** (`connecteurs/`) : `run_tool` ne sait pas non plus *où*
part une réservation. Il demande `connecteurs.pour(tenant)` et parle à une interface
commune — chercher un créneau, créer, retrouver par numéro, modifier, annuler. Deux
carnets : le nôtre (`reservations.py`, par défaut) et resOS (`connecteurs/resos.py`,
réglé par établissement dans l'admin, clé dans `RESOS_API_KEYS`). Tout ce qui n'est pas
une réponse sûre de resOS devient un refus que le modèle relit : il n'annonce jamais une
réservation que personne ne verra. Détails, hypothèses et limites : `docs/RESOS.md`.

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
| `tenants` | établissements : numéro, fiche, accueil, voix, formule, horaires (JSON), e-mail de notification, carnet de réservations, numéro de secours |
| `reservations` | réservations ; une annulée **reste**, horodatée (`cancelled_at`) |
| `calls` | journal des appels : durée, transcription, latences, journal de bord, coût par poste, et ce qu'est devenu un appel passé en secours |
| `messages` | messages pris pour l'équipe, à rappeler |
| `users` | super-admin et restaurateurs (bcrypt) |
| `supervision` | ardoise des mesures (relève Twilio, purge, sonde d'écriture) |

- **Hors de la boucle d'événements** : la boucle porte l'audio de tous les appels en
  cours. Tout accès SQLite du chemin d'appel et de l'admin passe par un fil
  (`db.hors_boucle`, ou un gestionnaire `def` que FastAPI exécute dans son pool). WAL,
  `synchronous=NORMAL`.
- **Heure du restaurant** : stockage en UTC, mais toutes les bornes (« aujourd'hui »,
  « ce mois-ci », « 30 derniers jours ») sont calculées à l'heure de Paris
  (`horloge.py`). Un appel à 00 h 30 le 1er compte dans le bon mois. **Tout ce que
  l'admin affiche** passe aussi par là (`horloge.au_restaurant`, filtres `date_paris`,
  `jour_paris`, `heure_paris`) : un gabarit ne découpe jamais un horodatage de la base.
- **Tâches de fond** : toutes passent par `taches.lancer` (référence retenue, exception
  journalisée, arrêt propre dans le `lifespan`).

### Pourquoi pas de RAG vectoriel

La fiche d'un restaurant tient en quelques milliers de caractères (plafond admin :
12 000) : elle est injectée entière dans le prompt système. Plus simple, plus fiable (pas
de passage manqué) et moins cher qu'un index vectoriel. Chemin d'upgrade le jour où un
établissement aura des documents volumineux : ingestion → découpage → embeddings, et
`build_system_prompt` injecte les passages retrouvés. L'interface ne change pas.

### Pourquoi Moshi a été abandonné, en deux temps

Le projet a démarré sur Moshi full-duplex (« speech-to-speech ») : pas d'appel d'outil
fiable, impossible à contraindre à la fiche du restaurant — or c'est le produit. D'où le
pipeline STT → LLM → TTS qu'on contrôle, où Moshi n'était plus que la **voix** (Moshi
1.6B de Kyutai, servie par `moshi-server` sur un GPU Modal).

Le 28/09/2026, la voix elle-même est passée à Mistral (Voxtral) : un GPU loué coûtait la
moitié d'un appel, mettait jusqu'à 46 s à se réveiller, et Modal n'en trouvait pas
toujours (pannes de capacité des 23, 24 et 25/09). Voxtral se facture au caractère et
répond sans réveil. Moshi reste la voix de secours quand la clé Mistral manque ; son
retrait est prévu (`docs/VOXTRAL.md`).

## Sécurité

| Menace | Parade |
|---|---|
| Requête forgée sur les webhooks ou le flux | `X-Twilio-Signature` vérifiée (`twilio_signature.py`), URL publique reconstruite depuis `PUBLIC_URL` ; mode `log` pour observer avant `enforce` |
| GPU de secours utilisé par un tiers | clé privée `MOSHI_TTS_API_KEY` posée au démarrage du conteneur Modal |
| Assistante en panne, client sans personne | renvoi vers le restaurant ou répondeur (`renvoi.py`) ; les adresses de secours exigent elles aussi la signature Twilio |
| Admin | session signée, CSRF, bcrypt, limitation des tentatives, cloisonnement par établissement (`deps.resolve_tenant`), CSP stricte (Caddy) |
| Exposition réseau | API liée à `127.0.0.1`, seul Caddy est public ; pare-feu `ufw` |
| Données personnelles | purges automatiques, droit à l'effacement, numéro tronqué dans les journaux |
| Perte de la base | sauvegarde nocturne vérifiée + copie hors du serveur (rclone), surveillées |

## Exploitation

- **Supervision** (`supervision.py`, [docs/SUPERVISION.md](docs/SUPERVISION.md)) : 17
  contrôles, une seule source pour l'écran « Santé & coûts » et la sonde `/supervision`.
  « Pas de mesure » n'est jamais un feu vert.
- **Garde-fous** : chaque protection critique a un test qui rougit quand on la retire
  (`scripts/mutation_check.py`, joué en CI).
- **Dépendances** : `requirements.lock` installé tel quel par l'image et la CI ;
  `ruff` et `pip-audit` en CI.
