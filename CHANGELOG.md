# Changelog

> **Sur la numérotation.** Les tags `v0.1.0` à `v0.6.0` ont été posés pendant la phase
> d'expérimentation, par deux personnes suivant deux logiques différentes : `v0.1.0`
> (19/07) est ainsi postérieur à `v0.5.0` (17/07), et la release GitHub `v0.2.0` décrit
> un contenu qui n'est pas celui de son tag. Ces tags sont publics, donc **ils ne sont
> pas réécrits** : les déplacer casserait toute référence existante pour un gain
> cosmétique. `v1.0.0` marque la reprise sur une numérotation cohérente.

## Non publiée — le carnet resOS (SCRUM-82 à 85, 25/09/2026)

Un établissement peut écrire ses réservations dans **resOS**, le logiciel de réservation du
restaurant, au lieu de notre base. resOS n'ayant ni bac à sable ni compte de test, tout est
développé contre un faux resOS calqué sur sa documentation publique (`docs/RESOS.md`).

### Ajouté
- **Un carnet par établissement** (`app/connecteurs/`) : `run_tool` parle à une interface
  commune, et le choix se fait dans la fiche de l'établissement (super-admin). Par défaut,
  et pour tout le parc existant : notre carnet, sans aucun changement.
- **resOS** : vérifier un créneau, créer, retrouver par numéro, modifier, annuler. Les
  réservations arrivent en **demandes à valider par le restaurant** (`request`, source
  `phone`), et l'assistante ne dit jamais « confirmé ».
- Tout ce qui n'est pas une réponse sûre de resOS (plus de 4 s, panne, clé absente ou
  refusée) devient un refus que le modèle relit : il **n'annonce rien** et prend un
  message. Le créneau est revérifié juste avant d'écrire, et le numéro de l'appelant est
  revérifié sur chaque réservation rendue par resOS.
- Un appel qui a réservé dans resOS garde la référence resOS (`calls.reservation_externe`)
  et compte parmi les appels avec réservation.

### À poser au déploiement
Rien pour le parc existant (migration v14 automatique). Pour un restaurant sous resOS :
`RESOS_API_KEYS=<id>=<clé>` dans le `.env`, puis « resOS » dans sa fiche.

## Non publiée — quand le GPU ne vient pas (25/09/2026)

Trois pannes de capacité chez Modal en deux jours (plus de GPU L4 en Europe : 23/09, 24/09 à
23 h 15, 25/09 à 8 h 48). Après 90 s de musique d'attente, la reprise partait vers un serveur
absent : l'appelant n'entendait plus **rien** et finissait par raccrocher (appels 158 et 159).

### Ajouté
- **Message « rappelez dans quelques minutes »** : si le GPU n'a pas répondu à la fin de
  l'attente (`MOSHI_HOLD_MAX_SECONDS`, 90 s), l'assistante s'excuse, demande de rappeler et
  raccroche. Le message est pré-rendu dans la voix de l'établissement, au même moment que
  l'accueil — c'est le seul moment où le serveur de voix est sûr de répondre. Il figure dans
  la transcription, et l'appel reste compté parmi les « Appels muets » : le filet amortit la
  panne, il ne la cache pas. `MOSHI_INDISPONIBLE_TEXT` remplace le texte.
- Le contrôle « Voix d'accueil » signale un message d'indisponibilité manquant.

### À poser au déploiement
Rien. Le message est rendu au démarrage, dès que le GPU répond (quelques secondes).

## Non publiée — ce que quatre vrais appels ont montré (20/09/2026)

Quatre appels passés le 20/09 au soir, réécoutés et relus en base (appels 138 à 141).
Rien ici ne vient d'une revue de code : ce sont quatre défauts qu'on entend.

### Corrigé
- **L'appelant qui changeait de langue n'était plus entendu du tout.** Une conversation
  commencée en français puis basculée en anglais rendait l'assistante sourde : la langue
  était tranchée une fois pour toutes et le STT verrouillé sur `fr`, où l'anglais ne
  produit rien. La langue se re-décide désormais en continu, et deux tours de parole sans
  la moindre transcription font revenir au bilingue — c'est le seul symptôme qui survive
  au verrou, puisque Deepgram verrouillé n'étiquette plus les langues.
- **L'accueil figurait deux fois** dans le contexte du modèle et dans la transcription
  rendue au restaurateur : la phrase de reprise était à la fois pré-inscrite et inscrite
  par l'agrégateur. Règle posée : on ne pré-inscrit que ce qui ne traverse pas le pipeline.
- **Un nom connu était épelé** au lieu d'être prononcé (« Is it H E L M I, like last
  time? ») : les noms tout en capitales sont remis en casse de titre, à l'écriture comme
  à la lecture, et le prompt le dit.
- **Résumé d'appel** : la colonne `calls.summary` n'avait jamais été écrite. Une phrase
  est produite après le raccroché, en tâche de fond, et remplace l'extrait brut dans la
  liste des appels et le tableau de bord. `RESUME_APPELS=0` coupe la fonction.

### À poser au déploiement
Rien d'obligatoire. `RESUME_APPELS` et `RESUME_MODEL` sont facultatifs (défauts : actif,
modèle de la conversation).

## v1.1.0 — déployée le 19/09/2026 — l'audit du 18/09/2026

Fusionnée dans `main` par la PR #96 et déployée sur le VPS le 19/09 (`scripts/deploy.sh`,
base migrée en v13).

### Pourquoi cette version
L'audit du 18/09 a relevé cinq défauts critiques : webhooks Twilio non authentifiés,
une route publique qui listait les réservations, un GPU ouvert avec la clé de
démonstration, des sauvegardes restées sur la machine, et une disponibilité toujours
« oui ». Cette version les ferme, et fait du produit un seul chemin d'appel.

### Sécurité
- **Signature Twilio** (`X-Twilio-Signature`) vérifiée sur les webhooks et la poignée de
  main du flux ; `PUBLIC_URL` ; mode `log` pour observer avant `enforce`.
- **Route publique `/tenants/{id}/reservations` supprimée.**
- **Clé privée du serveur de voix** posée au démarrage du conteneur Modal ; `modal
  deploy` refuse `public_token` ; contrôle de supervision.
- **Appels masqués** : les numéros de substitution de Twilio (`+266696687`…) ne sont plus
  pris pour un vrai numéro — tous les appels masqués partageaient leurs réservations.
- Jeton de supervision en en-tête seulement ; numéro de l'appelant tronqué dans les
  journaux ; saisies de l'admin vérifiées côté serveur (numéro E.164, dates, couverts).

### Ajouté
- **Horaires d'ouverture** par établissement, appliqués par le serveur : un créneau fermé
  est refusé avec les plages ouvertes du jour (écran « Horaires d'ouverture »).
- **E-mail au restaurateur** à chaque réservation, modification, annulation et message
  pris (SMTP générique, `SMTP_*`).
- **Nom du dernier passage** proposé au lieu d'être redemandé.
- **Copie des sauvegardes hors du serveur** (rclone), surveillée.
- Sonde de vie Docker (`HEALTHCHECK`), index SQLite v13, `ruff` et `pip-audit` en CI.
- **GPU chaud aux heures de service seulement** (`MOSHI_KEEPWARM_HEURES`), désactivé par défaut.

### Modifié
- **Un seul chemin d'appel** : Media Streams + Pipecat + Deepgram + moshi-server. Retirés :
  la boucle Gather/Say, Pocket TTS, Kyutai en PyTorch, Cartesia, l'ancienne app Modal
  (lisibles au tag `archive/moteurs-locaux`). Image 5,67 Go → 845 Mo.
- **Dépendances verrouillées** (`requirements.lock`), installées telles quelles par l'image
  et la CI.
- **Les fenêtres du tableau de bord commencent à minuit, heure de Paris** (et non plus
  UTC) : les totaux « aujourd'hui », « ce mois-ci » et les stats par jour peuvent bouger
  légèrement par rapport à v1.0.
- Coût Deepgram estimé au tarif `multi` (0,0092 $/min, borne haute) ; `docs/TARIFS.md`
  recalculé.
- « Annuler » dans l'admin annule la réservation (ligne barrée) au lieu de l'effacer.
- Supprimer un établissement supprime aussi ses messages et ses enregistrements.
- Le restaurant de démonstration n'est plus réaligné à chaque démarrage : l'admin fait
  foi (`SEED_DEMO`).
- **23 réglages atteignent enfin le conteneur** (GPU chaud, coûts, fuseau, délais…) :
  absents de `docker-compose.yml`, ils étaient sans effet même posés dans le `.env`.
- Accès SQLite hors de la boucle d'événements (appel et admin) ; tâches de fond
  retenues ; `lifespan` ; journaux loguru partout.

### À poser au déploiement
- `.env` du VPS : `PUBLIC_URL=https://app.helmane.fr`, `TWILIO_SIGNATURE=log` (48 h, puis
  retirer), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` ; le jeton
  Twilio **courant**.
- Clé du serveur de voix : `MOSHI_TTS_API_KEY` dans le `.env` local et celui du VPS,
  puis `modal deploy` (ordre dans `docs/MODAL.md`).
- `/opt/backups/backup.env` avec `RCLONE_REMOTE`, et le script de sauvegarde réinstallé.
- Dans l'admin : les horaires de chaque établissement.

## v1.0.x — août et septembre 2026 — déployé depuis `main`, sans tag

- Supervision extérieure : sonde `/supervision`, alerte GitHub Actions, relève des
  alertes Twilio (#24).
- Sauvegarde quotidienne vérifiée ; API publiée sur `127.0.0.1` seulement ; audit de
  sécurité de l'admin (#21).
- Grille tarifaire (#29), plafond mensuel qui ne coupe jamais la ligne (#31).
- RGPD : durées de conservation appliquées, droit à l'effacement (#22).
- Modification et annulation de réservation par téléphone (#33).
- Enregistrement des appels et journal de bord de l'interaction (#88).
- Messages pris pour l'équipe : la promesse de rappel laisse une trace (#32).
- Banc d'essai d'appels simultanés ; modèle de fin de tour partagé (#40).

## v1.0.0 — 2026-07-31 — Première version en production

### Pourquoi cette version
Premier jalon où le service tourne **en production, sur son propre domaine, validé par
des appels réels** : `https://app.helmane.fr`, sur un VPS parisien qui survit au reboot,
avec la marque Helmane. Les versions précédentes étaient des étapes techniques.

### Ajouté
- **Plateforme admin v3** : coquille à barre latérale, vue du parc et écran « Santé &
  coûts » pour le super-admin, salle de contrôle par établissement. Tout ce qui est
  affiché est mesuré — aucune métrique inventée.
- **Voix par établissement** (migration v5) : catalogue fermé de 7 voix françaises
  choisies à l'écoute, servies par le serveur Modal. La liste est fermée parce qu'une
  voix inconnue n'est PAS refusée par moshi-server : elle est remplacée en silence.
- **Mesure du blanc ressenti** tour par tour (migration v4), stockée en base — les
  journaux de conteneur, eux, repartent de zéro à chaque déploiement.
- **Numéro de l'appelant** enregistré (migration v3).
- **Domaine et HTTPS** : Caddy accepte plusieurs noms d'hôte, ce qui permet de basculer
  de domaine sans fenêtre de coupure sur le webhook Twilio.

### Corrigé
- **Panne de production** : un `reasoning` passé en mot-clé nu levait un `TypeError`
  dans le SDK OpenAI et rendait TOUS les appels muets. Le bon passage est `extra_body`.
- **Blanc de 6 s** avant chaque appel d'outil : le raisonnement de Gemini 2.5, actif par
  défaut. Coupé (`LLM_REASONING=off`) — 6,16 s → 0,59 s mesurés.
- **Coupures sur bruit** : Pipecat ouvrait un tour sur VAD *ou* transcription ; un souffle
  tranchait la phrase et rien ne repartait, d'où les « allô » du client
  (`INTERRUPTION=mots`).
- **Fuite entre établissements** : le nom des autres enseignes apparaissait dans les
  lignes de réservation d'un restaurateur après une édition.
- Relance incongrue après un « au revoir » : l'assistante raccroche désormais.

### Réglages figés après validation à l'oreille
`DEEPGRAM_MODEL=nova-3` (noms propres), `VAD_STOP_SECS=0.5`, `INTERRUPTION=mots`,
`LLM_REASONING=off`. Blanc médian mesuré : **1,16 s** (p90 1,58 s).

### Mesuré
Coût par appel : **11,4 ¢** en conditions réelles. 209 tests, aucun appel réseau.

---

## Archive — branche `claude/moshi-gpu-980ti-0w8lsv` (abandonnée) — Pocket TTS sur GPU

### Pourquoi
Sur CPU, `french_24l` met 5-10 s à produire son premier morceau → voix saccadée. Un
GPU (même une GTX 980 Ti / Maxwell) rend la génération temps réel (< 1 s).

### Ajouté
- `_select_device()` dans `pocket_tts.py` : `POCKET_TTS_DEVICE=auto` (défaut : GPU si
  dispo, sinon CPU) / `cuda` / `cpu`. Le modèle (`nn.Module`) est déplacé sur le GPU
  via `.to(device)` avant la préparation de l'état de voix.
- `api/Dockerfile.gpu` : PyTorch **2.5.1 + CUDA 12.1** épinglé — dernière lignée
  compatible Maxwell (sm_52) satisfaisant pocket-tts (>=2.5).
- `docker-compose.gpu.yml` : surcouche réservant un GPU NVIDIA + `POCKET_TTS_DEVICE=cuda`.
  Lancement : `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build`.
- `POCKET_TTS_DEVICE` dans le compose de base et `env.example` ; `docs/GPU.md`
  (prérequis nvidia-container-toolkit, lancement, vérification, note sur le 1.6B).
- 4 tests de sélection de device. 53 tests au total, toujours zéro appel réseau.

---

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
