# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Helmane : une assistante téléphonique pour restaurants (FastAPI + Pipecat + Twilio). Un
déploiement sert plusieurs établissements ; `main` tourne en production et prend de vrais
appels. Le code, les commentaires, les tests, les commits et la documentation sont **en
français** ; un commentaire dit le *pourquoi* (la panne vécue, la mesure), pas le quoi.

## Commandes

L'image `moshi-rag-voice-assistant-api` (`docker compose build`) contient les dépendances
verrouillées, mais ni les tests ni git : on monte le dépôt.

```bash
# Lancer l'application en local
cp env.example .env && docker compose up -d --build && curl -s localhost:8000/health

# Analyse statique + toute la suite (aucun réseau, environ 3 minutes)
docker run --rm --tmpfs /tmp:exec -v "$PWD:/repo" -w /repo/api moshi-rag-voice-assistant-api \
  sh -c "pip install -q pytest ruff >/dev/null 2>&1; ruff check --config ../ruff.toml app; \
         python -m pytest tests -q -p no:cacheprovider -W ignore"

# Un fichier, un test : même commande, en finissant par
#   python -m pytest tests/test_renvoi.py -q -k tourne_pas_en_rond

# Contrôle par mutation (environ 8 minutes) : retire chaque garde-fou, exige qu'un test rougisse.
# Il modifie puis restaure les sources : il refuse un arbre non committé.
docker run --rm --tmpfs /tmp:exec -v "$PWD:/repo" -w /repo moshi-rag-voice-assistant-api sh -c \
  "apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git >/dev/null 2>&1; \
   pip install -q pytest >/dev/null 2>&1; git config --global --add safe.directory /repo; \
   python -u scripts/mutation_check.py --runner native"

# Déploiement : seulement depuis `main`, CI verte, arbre propre — le script refuse sinon
bash scripts/deploy.sh --check && bash scripts/deploy.sh
```

La CI (`.github/workflows/ci.yml`, sur `main`, `claude/**` et chaque PR) installe
`api/app/requirements.lock`, puis joue `ruff check api/app` (règles E9 et F seulement),
`pip-audit`, la suite et `scripts/mutation_check.py`. `requirements.txt` est l'intention,
le `.lock` ce qui est installé (régénération : `docs/DEPLOY.md`).

## Cycle de travail

1. **Branche.** Jamais de commit sur `main`. Travailler sur une branche `claude/…` à jour
   de `main`. Avant chaque commit : `git branch --show-current` et `git status` —
   plusieurs sessions peuvent partager ce dossier.
2. **Test avec le code.** Une correction commence par le test qui reproduit le défaut ;
   une fonction arrive avec ses tests, dont un test de cloisonnement (403 entre deux
   établissements) pour toute ressource d'un établissement.
3. **Avant de committer** : analyse statique et suite complète vertes. Une page modifiée
   se regarde (skill `admin-ui`, « Vérifier un rendu »), en clair et en sombre, sur
   ordinateur et téléphone. Un comportement du chemin d'appel se prouve sur un vrai
   pipeline quand c'est possible, pas seulement sur des doublures.
4. **Protection critique** ajoutée ou déplacée (cloisonnement, signature, refus côté
   serveur, garde anti-boucle) : un garde-fou dans `scripts/mutation_check.py`, et le
   contrôle par mutation relancé après le commit.
5. **Commit** : un sujet par commit. Message en français, `CLÉ-JIRA · ce qui change`,
   corps = pourquoi et ce qui a été vérifié. Jamais de nom de modèle.
6. **Pousser, puis attendre la CI** de la branche. Rouge : corriger avant toute suite.
7. **Revue** avant de fusionner un lot : `/code-review` et `/security-review` sur le
   diff. Vérifier chaque constat dans le code avant d'y toucher ; ne corriger que ceux
   qui sont confirmés, par groupes, tests après chaque groupe ; dire ce qui est laissé de
   côté et pourquoi.
8. **PR vers `main`** : sections *Pourquoi*, *Ce qui change*, *Vérifié*, *Après le
   déploiement*. Fusion par commit de fusion, sur le commit que la CI a testé.
9. **Déploiement** : attendre la CI de `main`, sauvegarder les journaux du conteneur
   (ils s'effacent), lancer `scripts/deploy.sh`, vérifier dans le conteneur, recaler la
   branche de travail sur `main`. Le conteneur redémarre : déployer hors service.
10. **Recette** sur un vrai appel (`docs/RECETTE.md`), puis ticket Jira à « Terminé ».
    Tant que la recette n'est pas faite, le ticket reste « En cours de revue ».

Livrer, c'est aussi : une ligne dans `docs/RECETTE.md`, une entrée dans `CHANGELOG.md`,
la documentation concernée (`docs/SUPERVISION.md`, `docs/RGPD.md`…), et toute nouvelle
variable d'environnement dans `docker-compose.yml` et `env.example`.

**Ce qui se décide avec Helmi, jamais seul** : ouvrir une PR, fusionner, déployer, toute
écriture en production (base, console Twilio, `modal deploy`), clore un ticket. Lui rendre
compte en français, le verdict d'abord, en disant ce qui n'a **pas** été vérifié.

## Architecture

### Le chemin d'un appel (il n'y en a qu'un)

```
Twilio ─▶ POST /twilio/voice (signé) ─▶ TwiML <Connect><Stream>, puis <Redirect>/twilio/suite
       ─▶ WS /ws/voice ─▶ voice/bot.py:run_bot (Pipecat, μ-law 8 kHz)
            Deepgram nova-3 ─▶ LLM via OpenRouter (outils de llm.py) ─▶ voix Mistral Voxtral
```

- `main.py` : webhooks Twilio (tous derrière `twilio_signature.exiger`), flux média,
  `/health` (sonde de vie, sans auth) et `/supervision` (sonde d'état, 503 = alerte).
- `llm.py` : prompt système + six outils (`check_availability`, `create_reservation`,
  `find_reservation`, `modify_reservation`, `cancel_reservation`, `take_message`),
  partagés par la voix et le SMS. `llm.run_tool` est le **seul** endroit où un outil
  écrit ; les refus (créneau fermé, nom manquant) sont décidés par le serveur, pas par le
  modèle. La fiche de l'établissement est injectée entière dans le prompt (pas de RAG).
- `voice/` : `bot.py` assemble le pipeline. L'accueil est **pré-rendu** en WAV
  (`greeting.py`) pour un décroché sans blanc. `langue.py` décroche en multilingue puis
  se fixe ; `rattrapage.py` récupère une parole entendue mais non transcrite ;
  `enregistrement.py` ne doit jamais faire échouer un appel. `journal.py`, `latency.py`
  et `vigie.py` sont des **observateurs** : rien n'est inséré sur le chemin de l'audio.
- Voix : `voxtral_tts.py` (Mistral, catalogue dans `voices.py`). `moshi_server_tts.py`
  et `deploy/modal_moshi_server.py` (GPU Modal) ne sont plus qu'un secours à retirer.
- Panne : `renvoi.py` (décision, TwiML, mémoire des pannes), `voice/vigie.py` (deux
  erreurs de suite sur la voix, le modèle ou la transcription), `repondeur.py` (message
  vocal rapatrié de chez Twilio). L'assistante rend la ligne **sans raccrocher** et
  Twilio lit `/twilio/suite`. On n'envoie jamais de commande à Twilio pendant une panne.

### Établissements et données

- Le numéro appelé (`To`) désigne l'établissement : `tenants.get_by_phone`.
- SQLite, un fichier, sur le volume `api_data` avec les enregistrements et le cache
  d'accueil. Pas de SQL dans les routes : passer par `tenants.py`, `reservations.py`,
  `calls.py`, `messages.py`, `users.py`.
- Migrations : `db.py:_MIGRATIONS`, **en ajout seulement**, jouées au démarrage. Ne
  jamais éditer un script livré.
- `db.hors_boucle` pour tout accès SQLite depuis une fonction `async` : la boucle
  d'événements porte l'audio des appels en cours.
- `taches.lancer` pour toute tâche de fond, jamais `asyncio.create_task` nu.
- **Un seul worker uvicorn** : l'état vivant (pannes récentes, coupe-circuit resOS,
  caches) est en mémoire du processus.
- Une réservation annulée reste en base (`cancelled_at`). Le plafond d'une formule
  (`quotas.py`, `plans.py`) compte et prévient, mais ne coupe jamais la ligne.

### L'heure

La base stocke en UTC. Le fuseau du restaurant vit dans `horloge.py` : les bornes
(« aujourd'hui », « ce mois-ci ») se calculent avec `horloge.il_y_a_jours` et
`horloge.debut_du_mois`, jamais avec `date('now')` en SQL. Tout ce qui s'affiche passe
par `horloge.au_restaurant` ou les filtres Jinja `date_paris`, `jour_paris`, `heure_paris`.

### Carnets de réservations

`connecteurs.pour` rend le carnet de l'établissement : `interne` (notre table), `resos`
(API resOS, clé dans `RESOS_API_KEYS`) ou `resos_demo` (`connecteurs/bac_a_sable.py`, un
faux resOS dans le processus). Pour un établissement resOS, resOS fait foi : l'admin le
lit, ne l'écrit pas. `connecteurs/sante.py` porte le coupe-circuit. Voir `docs/RESOS.md`.

### Site et rappel (`api/app/site/`, `rappel.py`)

La racine `/` sert la page d'accueil publique, sans session ni accès à la base ; ses prix
viennent de `plans.py`. « Rappelez-moi » fait composer un numéro par Twilio à la demande
d'un inconnu : tout ce qui décide si l'appel part est dans `rappel.demander` (numéros de
métropole seulement, plafonds par numéro, par adresse et par jour, pas d'appel la nuit).
Quand la personne décroche, Twilio lit `/twilio/rappel` (signée) ; `run_bot` reçoit
`demonstration=True` et l'appel est noté `sortant`, chiffré au tarif sortant.

### Admin (`api/app/admin/`)

FastAPI + Jinja2 + htmx + Pico, rendu côté serveur. **Charger le skill `admin-ui` avant
toute page, route, gabarit, style ou script** : il porte les règles de sécurité, les
motifs d'interface, les pièges connus et la façon de vérifier un rendu. Le skill
`dataviz` avant un graphique.

### Coûts, supervision, RGPD

- `calls.py` chiffre chaque appel à sa clôture, poste par poste (tarifs `COST_*`) ; la
  répartition de « Santé & coûts » est la somme de ces colonnes, pas une estimation.
- `supervision.py` : une liste ordonnée de contrôles, même source pour l'admin et la
  sonde. « Pas de mesure » n'est jamais un feu vert.
- `rgpd.py` : purges par durée de conservation. Toute nouvelle donnée personnelle a sa
  ligne au registre de `docs/RGPD.md`.

## Ce que les tests imposent

Ces règles ne sont pas des conventions : un test échoue si on les oublie.

| Règle | Test |
|---|---|
| Toute variable d'environnement lue par `api/app` est déclarée dans `docker-compose.yml` | `test_supervision.py::TestCablage` |
| Aucune page d'admin n'ouvre la base depuis la boucle d'événements (ajouter toute nouvelle page à la liste) | `test_db_hors_boucle.py` |
| Aucun gabarit ne découpe un horodatage de la base | `test_heure_de_paris.py` |
| Le thème sombre choisi est identique au sombre automatique | `test_theme.py` |
| Un nouveau contrôle de supervision est déclaré dans `ATTENDUS`, dans `docs/SUPERVISION.md` et dans le compte du `README.md` | `test_supervision.py`, `test_documentation.py` |
| La page d'accueil n'a ni script en ligne ni ressource d'un autre site, et ses prix sont ceux de `plans.py` | `test_site.py` |
| Ce fichier et le skill `admin-ui` ne citent que des fichiers et des noms qui existent | `test_documentation.py` |
| Chaque protection critique fait rougir un test quand on la retire | `scripts/mutation_check.py` |

L'ancre `avant` d'un garde-fou de `GARDE_FOUS` doit apparaître **exactement une fois**
dans son fichier : déplacer ou reformuler cette ligne casse le contrôle, il faut alors
mettre l'ancre à jour.

Les tests ne touchent jamais le réseau (`conftest.py` pose l'environnement avant l'import
de l'application et remet à zéro la mémoire des pannes). Leurs noms disent le
comportement attendu, en français. `test_vigie.py::TestAvecLeVraiPipeline` est le modèle
d'un test qui fait tourner un vrai pipeline Pipecat. Le faux resOS écrit son état sur
disque : une fixture qui l'utilise efface son fichier en sortie.

## Pièges connus (Pipecat 1.5)

- Le sérialiseur Twilio **raccroche l'appel** (API REST) quand le pipeline s'arrête.
  Pour rendre la ligne sans raccrocher : `bot.serialiseur_twilio` et `garder_la_ligne`.
- Une erreur rendue par une synthèse n'atteint les observateurs que ~3 s plus tard.
- Ne pas arrêter le pipeline depuis un observateur (il s'attendrait lui-même) : passer
  par `taches.lancer`, comme `bot.passer_la_main`.
- L'audio émis avant le démarrage du pipeline est jeté : attendre `on_pipeline_started`.

## Règles permanentes

- **`main` est la production.** `scripts/deploy.sh` vérifie la CI du commit exact, la
  santé, que l'API n'est pas joignable hors Caddy, et que la supervision répond ; on ne
  déploie jamais par un `ssh` + `git checkout`.
- **Secrets** : dans `.env` seulement. `env.example` ne porte que des noms et des valeurs
  par défaut. Ne jamais afficher la valeur d'un secret (journal, commit, PR, réponse) :
  vérifier une présence, comparer des empreintes.
- **Aucun chiffre inventé** : ce qui est affiché ou écrit dans la documentation se lit
  en base, se mesure, ou se relève chez le fournisseur — avec sa date.
- **Ne jamais affirmer ce qu'on ne sait pas**, dans une page comme dans un compte rendu.
- Le suivi du travail est dans Jira (projet `ASSISTANTE`, anciennes clés `SCRUM-n`) :
  « À faire », « En cours », « En cours de revue », « Terminé ». Les `#nn` du code
  renvoient aux anciennes issues GitHub.

## Quels documents croire

| À jour | Sujet |
|---|---|
| `README.md`, `ARCHITECTURE.md` | le produit ; les choix techniques et leurs raisons |
| `CHANGELOG.md` | ce qui a été livré, et pourquoi |
| `docs/RECETTE.md` | ce qui se vérifie à la main, par fonction |
| `docs/DEPLOY.md`, `docs/TWILIO_SETUP.md`, `docs/SUPERVISION.md`, `docs/RGPD.md` | exploitation |
| `docs/RESOS.md`, `docs/VOXTRAL.md`, `docs/TARIFS.md` | carnet resOS, voix Mistral, grille tarifaire |

Historiques, à ne pas suivre : `ROADMAP.md` (état du 19/09/2026), `docs/PASSATION.md`
(août 2026), `docs/VOICE_STACK.md` (étude de juillet 2026), `docs/MODAL.md` (la voix de
secours Moshi), `docs/GITHUB_SETUP.md`.
