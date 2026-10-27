# Déploiement — hébergement 24/7 sur VPS (production)

> But : faire tourner l'app **en continu sur un serveur cloud EU**, avec un **domaine
> stable + HTTPS**, pour que la ligne téléphonique soit disponible 24/7 **sans dépendre
> d'un PC ni de ngrok**. Modal reste dédié au TTS GPU (voix Moshi). C'est la Phase 2 de
> `docs/PASSATION.md`.
>
> Architecture : `Twilio → https://votre-domaine (Caddy HTTPS → api:8000, FastAPI+Pipecat,
> SQLite) → websocket → Modal GPU EU (moshi-server)`.

## Prérequis

1. **VPS** en **région EU** (Hostinger ou équivalent), Ubuntu 22.04+, 2 vCPU / 4 Go conseillés.
2. **Domaine ou sous-domaine** (ex. `assistant.mondomaine.fr`) dont l'enregistrement DNS **A**
   pointe sur l'IP du VPS. Indispensable : Twilio exige un webhook stable et Let's Encrypt ne
   certifie pas une IP nue.
3. **Clés fraîches** (rotation) : Twilio auth token, Deepgram, OpenRouter — ne jamais réutiliser
   une clé qui a été exposée. Le serveur Modal doit être déployé (voir `docs/MODAL.md`).

## 1 — VPS + Docker

```bash
# sur le VPS, en root ou sudo
apt-get update && apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh          # Docker + Compose plugin
docker --version && docker compose version       # vérifier
```

## 2 — DNS

Créer un enregistrement **A** : `assistant.mondomaine.fr → <IP du VPS>`. Vérifier la propagation :
```bash
dig +short assistant.mondomaine.fr               # doit renvoyer l'IP du VPS
```
Ouvrir les ports **80** et **443** (Let's Encrypt valide via le port 80).

## 3 — Cloner + configurer

```bash
git clone https://github.com/helmi75/moshi-rag-voice-assistant.git
cd moshi-rag-voice-assistant
cp env.example .env
nano .env
```

`.env` de production — valeurs minimales :
```
SITE_ADDRESS=assistant.mondomaine.fr             # -> Caddy fait le HTTPS tout seul
PUBLIC_WS_URL=wss://assistant.mondomaine.fr/ws/voice

DEEPGRAM_API_KEY=<clé fraîche>

MISTRAL_API_KEY=<clé Mistral>                   # la voix (docs/VOXTRAL.md)
# Voix de secours, seulement si la clé Mistral manque (abandon prévu) :
MOSHI_TTS_URL=wss://<vous>--moshi-server-tts-server.modal.run
MOSHI_TTS_API_KEY=<openssl rand -hex 32>       # même valeur que pour modal deploy (docs/MODAL.md)

LLM_MODEL=google/gemini-2.5-flash
OPENROUTER_API_KEY=<clé fraîche>

TWILIO_ACCOUNT_SID=<...>
TWILIO_AUTH_TOKEN=<clé fraîche>
TWILIO_NUMBER=<+33...>

# Admin
ADMIN_EMAIL=vous@exemple.fr
ADMIN_PASSWORD=<mot de passe fort>
SESSION_SECRET=<openssl rand -hex 32>
SESSION_SECURE=1                                 # cookies HTTPS-only (domaine en place)

# Naturalité (défauts déjà validés)
USER_TURN_STOP_TIMEOUT=1.0
VAD_CONFIDENCE=0.85
VAD_STOP_SECS=0.4
```

## 4 — Démarrer

Première installation seulement (ensuite, `scripts/deploy.sh` lance l'image publiée par la CI,
voir « Exploitation », **Image**) :

```bash
docker compose up -d --build
curl -s https://assistant.mondomaine.fr/health   # attendu: {"status":"ok",...} en HTTPS
```
Au 1er démarrage, Caddy obtient le certificat Let's Encrypt (quelques secondes). `restart:
unless-stopped` est déjà en place : la pile **remonte seule** après un reboot.

## 5 — Pointer Twilio

Dans la console Twilio, le numéro → **Voice → A call comes in → Webhook** :
```
https://assistant.mondomaine.fr/twilio/voice   (HTTP POST)
```
Fini ngrok. L'URL ne changera plus.

## 6 — Modal (voix de secours Moshi, facultatif)

> Depuis le 28/09/2026 la voix vient de Mistral : cette étape ne sert qu'à garder la voix
> de **secours**, utilisée seulement si `MISTRAL_API_KEY` manque. Son retrait est prévu.

Le serveur Modal est déjà en **région EU** par défaut (`MODAL_REGION=eu`). Redéployer si besoin :
```bash
modal deploy deploy/modal_moshi_server.py
```
**Cold start / GPU chaud** (`MODAL_MIN_CONTAINERS`, à décider selon le budget) :
- **Scale-to-zero** (défaut, recommandé au lancement) : `0`. Gratuit à vide ; le 1er appel après
  une pause attend ~46 s, **couvert par l'accueil pré-enregistré + la musique d'attente**.
- **GPU chaud aux heures de service** (côté application, sans redéployer Modal) :
  `MOSHI_KEEPWARM_SECONDS=90` et `MOSHI_KEEPWARM_HEURES=11:30-14:30,18:30-23` dans le `.env`,
  puis `docker compose up -d api`. ≈ 0,80 $ par heure chaude : 7 h 30 par jour ≈ 180 $/mois.
  Le GPU s'éteint la nuit ; un appel hors plage retrouve le démarrage à froid couvert.
- **GPU chaud 24/7** : `1` → 0 cold start, mais ~250-290 €/mois (L4 24/7). À activer quand un client
  payant le justifie : `MODAL_MIN_CONTAINERS=1 modal deploy deploy/modal_moshi_server.py`.

## Vérification (bout en bout)

1. `curl https://assistant.mondomaine.fr/health` → 200, certificat HTTPS valide.
2. Admin : `https://assistant.mondomaine.fr/admin/login` → connexion OK, design en place.
3. **Appel réel** sur le numéro Twilio : accueil → conversation → **réservation enregistrée** ;
   transcript propre dans l'admin.
4. **Résilience (le critère qui compte)** : `sudo reboot` → au retour, `docker compose ps` montre
   `api` et `caddy` **Up** sans rien lancer, et la ligne re-répond. Plus aucune dépendance au PC.

## Exploitation

- **Logs** : `docker compose logs -f api` (appels, transcripts, coûts).
- **Sauvegarde** de la base : voir la section dédiée ci-dessous. Ne pas copier le fichier à chaud.
- **Supervision** : `docs/SUPERVISION.md`. La sonde `/supervision` exige `SUPERVISION_TOKEN`
  dans le `.env` du serveur ET le même secret côté GitHub — sans les deux, rien n'est surveillé.
- **Mise à jour** : depuis ton poste, `scripts/deploy.sh` — **jamais** un `git pull` à la
  main sur le VPS. Le script refuse de déployer si tu n'es pas sur `main`, si l'arbre est
  sale, si ton HEAD diffère de `origin/main`, ou si la CI n'est pas verte **pour ce commit
  exact** (et non « la dernière CI de la branche »). Il déploie ensuite par
  `reset --hard` sur le SHA, puis vérifie `/health` et le commit réellement en place.
  `scripts/deploy.sh --check` contrôle les conditions sans rien changer.
- **Image** (ASSISTANTE-167) : le serveur ne construit plus l'application. Sur `main`, la CI
  construit l'image après les tests, vérifie qu'elle démarre et répond sur `/health`, puis
  la publie sur GHCR, privée : `ghcr.io/helmi75/moshi-rag-voice-assistant:sha-<commit>`.
  `scripts/deploy.sh` la télécharge **avant** de toucher à quoi que ce soit (refus net si
  elle manque), vérifie que sa révision est le commit déployé, la nomme
  `moshi-rag-voice-assistant-api:latest` et lance `docker compose up -d --no-build`. Il
  contrôle ensuite que le conteneur qui tourne porte cette révision, et ne garde que les cinq
  dernières images (environ 1,2 Go chacune). **Ne jamais lancer `--build` sur le serveur** :
  l'image ne serait plus celle que la CI a éprouvée.

  Une fois pour toutes, le serveur doit pouvoir lire le registre privé. Créer un jeton
  (classic) avec la **seule** permission `read:packages` (GitHub → Settings → Developer
  settings → Personal access tokens → Tokens (classic)), puis sur le serveur :

  ```bash
  docker login ghcr.io -u helmi75      # coller le jeton quand il est demandé
  ```

  Le jeton reste dans `/root/.docker/config.json` (encodé, pas chiffré) : il ne permet que de
  lire les images. À son expiration, `scripts/deploy.sh` refuse de déployer et le dit.

  La règle « la production, c'est `main` après CI verte » ne tenait que par la discipline :
  c'est ainsi qu'on s'est retrouvé avec une production tournant sur une branche jamais
  fusionnée. Le script la rend mécanique.

## Signature des requêtes Twilio

Les webhooks (`/twilio/voice`, `/twilio/sms`, `/twilio/webhook`) et la poignée de main du
flux (`/ws/voice`) vérifient `X-Twilio-Signature` (`api/app/twilio_signature.py`). Sans
ça, le numéro d'un restaurant étant public, n'importe qui pouvait faire parler
l'assistante et payer le LLM ou le GPU.

Twilio signe l'URL **publique** ; derrière Caddy, l'application voit `http://api:8000/…`.
D'où `PUBLIC_URL` — l'origine exacte configurée dans la console Twilio :

1. dans le `.env` : `PUBLIC_URL=https://assistant.mondomaine.fr` et, pour la première
   mise en service, `TWILIO_SIGNATURE=log` ;
2. déployer, passer deux ou trois appels ;
3. lire la sonde : contrôle « Signature des requêtes Twilio » — `acceptées > 0`,
   `refusées = 0` (webhooks **et** poignée de main) ;
4. après 48 h sans refus, retirer la ligne `TWILIO_SIGNATURE=log` et redéployer :
   le défaut est `enforce` dès qu'un jeton existe.

En `enforce`, « toutes les requêtes refusées » est une **panne** de la sonde : ce n'est
pas une attaque, c'est l'URL reconstruite (ou le jeton) qui ne correspond plus à ce que
Twilio signe — remettre `log` le temps de corriger. Le jeton doit être l'*Auth Token*
courant du compte : un jeton renouvelé dans la console et pas dans le `.env` refuse tout.

## Notifications au restaurateur

Chaque réservation prise, modifiée ou annulée par téléphone, et chaque message pris,
déclenche un e-mail (`api/app/notifications.py`) vers les comptes restaurateurs de
l'établissement et l'adresse de notification de sa fiche. Cinq variables dans le `.env`
(`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, plus `SMTP_SSL=1` pour
un port 465), puis `docker compose up -d api`. L'envoi part en tâche de fond : un SMTP en
panne se lit dans les journaux, jamais dans l'appel. Essai sans téléphoner :

```bash
docker compose exec api python -c "import asyncio; from app import notifications, tenants; \
  print(asyncio.run(notifications.notifier(tenants.get_by_id(1), 'message_pris', \
  {'subject': 'Essai SMTP', 'details': 'Si vous lisez ceci, ça marche.'})))"
```

## Dépendances figées (`api/app/requirements.lock`)

L'image et la CI installent `requirements.lock`, la liste **exacte** des versions ;
`requirements.txt` n'est que l'intention. Sans ce verrou, chaque `docker compose up --build`
réinstallait « la dernière » version de FastAPI, Starlette, OpenAI, websockets… — la
production pouvait casser sans un commit. Régénérer (avec [uv](https://docs.astral.sh/uv/)) :

```bash
uv pip compile api/app/requirements.txt -o api/app/requirements.lock \
    --python-version 3.12 --python-platform linux --no-annotate --no-header --upgrade
```
(puis remettre l'en-tête de commentaire du fichier, qui documente cette commande).
(`--upgrade-package pipecat-ai` pour ne monter qu'un paquet.) Puis : suite de tests,
construction de l'image, et la recette (`docs/RECETTE.md`) avant de déployer.

**Actions épinglées (ASSISTANTE-178).** Chaque `uses:` des workflows désigne une action par
l'empreinte complète de son commit, suivie de `# vX.Y.Z` : une étiquette (`@v4`) peut être
déplacée par son auteur, et le workflow de déploiement tient une clé root du serveur.
Dependabot (`.github/dependabot.yml`) ouvre chaque semaine une PR pour les montées ; elle
passe la CI et la revue comme les autres. Épingler une nouvelle action : lire l'empreinte de
l'étiquette exacte chez son auteur (`git ls-remote https://github.com/<auteur>/<action>
refs/tags/vX.Y.Z`), jamais celle d'une branche. `test_workflows.py` refuse une action non
épinglée, une empreinte sans sa version, et un workflow sans `permissions:` de premier
niveau.

**Avis de sécurité (ASSISTANTE-177).** La CI passe le verrou à `pip-audit`, et un avis
la rend **rouge**. Deux réponses :

1. Monter le paquet : `--upgrade-package <paquet>` en gardant le verrou existant comme
   point de départ (copié dans le fichier de sortie), pour que rien d'autre ne bouge.
2. Si la correction est hors d'atteinte (un paquet qui en bloque un autre), l'accepter par
   écrit dans `.github/pip-audit-exceptions.txt` : un bloc `# paquet==version : raison`,
   puis un identifiant par ligne. La raison dit pourquoi l'application n'est pas touchée,
   vérifiée dans le code. `test_audit_dependances.py` rougit dès que le paquet monte : le
   bloc est alors à revoir.

Depuis le 27/10/2026 (ASSISTANTE-179), **aucune exception** : la montée de Pipecat en 1.14
a emporté les trois blocs d'ASSISTANTE-177 (`nltk`, le runner de `pipecat-ai`, `pillow`
12.3). Ils restent lisibles dans l'historique git du fichier.

## Sauvegardes

La base vit dans le volume `api_data`, sur la machine, en un seul exemplaire. Une
réservation perdue est un client perdu : c'est le seul incident du projet qui ne se
rattrape pas.

**Ne jamais copier le fichier à chaud** (`cp`, `docker compose cp`). La base est
vivante ; copier pendant qu'une réservation s'écrit produit un fichier corrompu, et
on ne s'en aperçoit que le jour où on essaie de le restaurer. `scripts/backup-db.sh`
utilise `.backup`, l'API de sauvegarde en ligne de SQLite, qui gère le verrouillage.

Installation sur le VPS, en root :

```bash
apt-get install -y sqlite3
install -D -m 755 scripts/backup-db.sh /opt/backups/backup-db.sh
( crontab -l 2>/dev/null; \
  echo "0 4 * * * /opt/backups/backup-db.sh >> /var/log/helmane-backup.log 2>&1" ) | crontab -
```

Le script vérifie l'intégrité de la copie **avant** de la nommer définitivement : un
fichier `app-*.db.gz` présent est donc restaurable par construction. Rétention 14
jours (`RETENTION_JOURS`), chemin de base surchargeable par `DB`.

**Test de restauration — à faire au moins une fois.** Une sauvegarde jamais restaurée
n'est pas une sauvegarde :

```bash
LAST=$(ls -1t /opt/backups/db/app-*.db.gz | head -1)
zcat "$LAST" > /tmp/restore-test.db
sqlite3 /tmp/restore-test.db "PRAGMA integrity_check; \
  SELECT count(*) FROM reservations; SELECT count(*) FROM tenants;"
rm -f /tmp/restore-test.db
```

Restauration réelle (arrêter l'app d'abord, sinon l'écriture en cours écrase la copie) :

```bash
cd /opt/moshi-rag-voice-assistant
docker compose stop api
zcat /opt/backups/db/app-AAAAMMJJ-HHMM.db.gz \
  > /var/lib/docker/volumes/moshi-rag-voice-assistant_api_data/_data/app.db
docker compose start api
```

### Réplication continue (Litestream, ASSISTANTE-158)

La sauvegarde de 4 h laisse jusqu'à une journée de réservations à découvert. Le service
`litestream` (image épinglée `litestream/litestream:0.3.13`, `deploy/litestream.yml`) envoie
le journal de la base (WAL) à un stockage objet chaque seconde : la perte visée tombe à
quelques secondes. Il est **éteint par défaut** (profil Compose `replication`) : tant que
`COMPOSE_PROFILES` n'est pas posé, rien ne tourne et rien ne change.

**Activer** (Helmi, après avoir ouvert un stockage objet **dans l'UE** : le choix du
fournisseur est le sien, et son prix à relever à l'ouverture) :

```bash
# .env du serveur
COMPOSE_PROFILES=replication
LITESTREAM_REPLICA_URL=s3://seau/helmane          # seau/dossier
LITESTREAM_ACCESS_KEY_ID=...
LITESTREAM_SECRET_ACCESS_KEY=...
LITESTREAM_ENDPOINT=https://...                    # vide pour Amazon S3
LITESTREAM_PATH_STYLE=false                        # true si le fournisseur l'exige
docker compose up -d --no-build                    # (ou le prochain deploy.sh)
docker compose logs litestream | tail              # « wal segment written »
```

`scripts/deploy.sh` relance `docker compose up -d --no-build` : sans le profil, le service
n'existe pas pour lui ; avec, il est lancé comme le reste. Le contrôle « Réplication continue
de la base » (docs/SUPERVISION.md) passe de « Non configurée » à « Réplique à jour ». Le
service lit la base sans jamais l'écrire ; il a ses métriques sur le réseau Docker (`:9090`),
aucun port n'est publié.

**Restaurer** (machine perdue, ou base corrompue). L'API arrêtée, la base restaurée dans un
dossier vide **d'abord**, vérifiée, puis seulement mise en place :

```bash
cd /opt/moshi-rag-voice-assistant
docker compose stop api litestream
mkdir -p /tmp/restauration
docker compose run --rm --no-deps -v /tmp/restauration:/restauration litestream \
  restore -config /etc/litestream.yml -o /restauration/app.db /app/data/app.db
sqlite3 /tmp/restauration/app.db "PRAGMA integrity_check; SELECT count(*) FROM reservations;"
# si c'est bon : remplacer la base du volume, puis relancer
cp /tmp/restauration/app.db /var/lib/docker/volumes/moshi-rag-voice-assistant_api_data/_data/app.db
rm -f /var/lib/docker/volumes/moshi-rag-voice-assistant_api_data/_data/app.db-wal \
      /var/lib/docker/volumes/moshi-rag-voice-assistant_api_data/_data/app.db-shm
docker compose up -d --no-build
```

Pour un point dans le temps : `-timestamp 2026-11-02T10:00:00Z` après `restore`. La commande `restore` a été éprouvée par `docker run` avec la même configuration ; le
enrobage `docker compose run` ci-dessus ne l'a pas été sur un serveur. Les fichiers
restaurés appartiennent à root (le conteneur y tourne) et sont en `0600`.

**Éprouvé en local le 27/10/2026**, sur un MinIO jetable : base de 800 lignes en WAL avec
écritures en continu, `restore` dans un dossier vide, `PRAGMA integrity_check` = `ok`, 800
lignes retrouvées sur 800, en **6,4 s** conteneur compris. Cette durée est celle d'une
petite base sur le réseau local, **pas** celle d'une vraie restauration depuis un stockage
distant : elle croît avec la taille de la base et le débit du fournisseur, et reste à
mesurer sur la vraie réplique (ligne S13 de `docs/RECETTE.md`).

Les sauvegardes quotidiennes (`backup-db.sh`) restent utiles — fichier unique,
restaurable sans Litestream — et peuvent viser **le même stockage** : `RCLONE_REMOTE`
(ci-dessous, ASSISTANTE-80) accepte un autre dossier du même seau.

### Copie hors du serveur

Les copies locales protègent d'une erreur de manipulation, pas de la perte de la machine :
un incident disque emporte l'original **et** les sauvegardes. Le script envoie donc aussi
chaque archive vérifiée vers un stockage objet (UE) via `rclone`, dès que `RCLONE_REMOTE`
est posé :

```bash
apt-get install -y rclone
rclone config                       # créer le remote (ex. « sauvegardes », S3 Scaleway/OVH)
cat > /opt/backups/backup.env <<'EOF'
RCLONE_REMOTE=sauvegardes:helmane-db
# RETENTION_DISTANTE_JOURS=30
EOF
chmod 600 /opt/backups/backup.env
/opt/backups/backup-db.sh           # doit finir par « copie distante ok »
rclone ls sauvegardes:helmane-db
```

`backup.env` reste sur la machine (jamais dans le dépôt). Les identifiants du stockage
vivent dans `~/.config/rclone/rclone.conf` de root. Le script écrit un second jeton,
`derniere-sauvegarde-distante`, **seulement** quand l'archive est confirmée sur le remote ;
la supervision (contrôle « Sauvegarde ») passe en attention s'il manque ou vieillit.

Restaurer depuis le stockage distant (machine perdue) :

```bash
rclone copy sauvegardes:helmane-db/app-AAAAMMJJ-HHMM.db.gz /tmp/
zcat /tmp/app-AAAAMMJJ-HHMM.db.gz > /tmp/restore-test.db
sqlite3 /tmp/restore-test.db "PRAGMA integrity_check;"
```

## Préproduction (ASSISTANTE-174)

`https://preprod.helmane.fr` : la même image que la production, sur le même serveur, pour
voir une version démarrer, migrer sa base et servir ses pages **avant** la production. Elle
n'a **ni numéro ni clé de fournisseur** (décision du 26/10/2026) : elle ne prend aucun
appel et ne dépense rien. Les appels se vérifient toujours en production.

| | Production | Préproduction |
|---|---|---|
| Copie du dépôt | `/opt/moshi-rag-voice-assistant` | `/opt/helmane-preprod` |
| Conteneur | `api` | `api_preprod` (`docker-compose.preprod.yml`) |
| Base | volume `moshi-rag-voice-assistant_api_data` | volume `helmane-preprod_api_data` |
| Port, boucle locale | 8000 | 8001 |
| Déploiement | `scripts/deploy.sh` | `scripts/deploy_preprod.sh [<commit>]` |

Le conteneur de préproduction rejoint le réseau Docker de la production pour que le même
Caddy le serve (bloc `PREPROD_ADDRESS` de `caddy/Caddyfile`). D'où son nom, `api_preprod` :
deux conteneurs `api` sur ce réseau se partageraient le nom. Conséquence : tant qu'il
tourne, `docker compose down` côté production ne peut pas retirer le réseau. Et la
préproduction joint l'API de la production sans passer par Caddy : il faudrait d'abord y
exécuter du code, mais elle pourrait alors forger `X-Forwarded-For`, que lit le frein des
connexions à l'admin (`admin/throttle.py`).

`scripts/deploy_preprod.sh` n'accepte qu'un commit de `main` dont la CI est verte, télécharge
son image, la lance (sans ménage : seul `scripts/deploy.sh` retire des images, après une
vérification réussie), puis vérifie : `/health` sur le serveur, l'image du conteneur, le port
8001 fermé vu de l'extérieur, la sonde qui répond (503 attendu : sans numéro ni clé, des
contrôles sont en panne), et l'adresse publique. **Pas de retour arrière** : une
préproduction qui ne démarre pas est ce qu'on voulait apprendre avant la production.

**Mise en place, une fois** (Helmi) :

1. DNS : un enregistrement `A` `preprod` → `187.77.172.87`.
2. Une fois ce lot déployé en production, ajouter `PREPROD_ADDRESS=preprod.helmane.fr` au
   `.env` de la production, puis, **hors appel en cours** (Caddy redémarre) :
   `cd /opt/moshi-rag-voice-assistant && docker compose up -d caddy`.
3. Depuis le poste : `bash scripts/deploy_preprod.sh --installer`. Il clone le dépôt dans
   `/opt/helmane-preprod` et y écrit un `.env` sans numéro ni clé ; ses secrets sont tirés
   sur le serveur et n'en sortent pas. Pour ouvrir l'admin de la préproduction, y ajouter
   `ADMIN_PASSWORD=…` à la main.
4. `bash scripts/deploy_preprod.sh`.

Le déploiement automatique après chaque fusion viendra avec le workflow de déploiement
(étape 5 du chantier « livraison »).

## Versions (ASSISTANTE-175)

Releases groupées (décision du 24/10/2026) : une version rassemble plusieurs déploiements.
Le CHANGELOG fait foi ; la release GitHub le recopie.

1. Sur une branche : `python3 scripts/notes_de_version.py preparer vX.Y.Z`. Les sections
   « Non publiée » du haut du CHANGELOG passent sous `## vX.Y.Z — date`. PR, puis fusion.
2. **Le tag vaut accord de Helmi** : `git tag vX.Y.Z <commit de main> && git push origin vX.Y.Z`.
3. Le workflow « Version » (`.github/workflows/release.yml`) vérifie que le commit est sur
   `main`, tire les notes du CHANGELOG, donne à l'image `sha-<commit>` l'étiquette `vX.Y.Z`
   sur GHCR (même empreinte, rien n'est reconstruit ; refus si la CI de `main` n'a pas
   publié d'image pour ce commit) et crée la release.

Une version ne se déploie pas toute seule : `bash scripts/deploy.sh --version vX.Y.Z` remet
en service le commit du tag, avec les mêmes vérifications.

## Depuis GitHub (ASSISTANTE-176)

Le workflow « Déploiement » (`.github/workflows/deploiement.yml`) lance les **mêmes
scripts** depuis GitHub : « Run workflow », cible `production` ou `preproduction`, et un
commit ou tag de `main` (obligatoire pour la production : c'est lui qu'on approuve ; vide en
préproduction : le dernier). La préproduction peut aussi partir seule après chaque CI verte
de `main`. Un seul déploiement à la fois ; GitHub ne garde qu'un passage en attente, un
troisième annule celui qui attendait.

Deux garde-fous de `scripts/deploy.sh` servent d'abord ce cas, où personne ne regarde :

- **Aucun appel coupé : la vidange (ASSISTANTE-159).** Juste avant le lancement, le script
  pose un drapeau `vidange` dans le volume de données, à côté de la base (écrit par
  `docker compose exec`, sans route ni secret). Tant qu'il est là, chaque NOUVEL appel est
  servi comme pendant une panne : renvoyé au restaurant, ou au répondeur
  (`app/renvoi.py`, motif `vidange`). Le script lit la jauge `helmane_appels_en_cours` de
  `/metrics` (jeton `METRIQUES_TOKEN` du `.env` du serveur) toutes les 5 s, et lance la
  nouvelle version dès qu'elle tombe à zéro. Au-delà de `VIDANGE_ATTENTE` (600 s par
  défaut), il refuse. La nouvelle version efface le drapeau en démarrant, et la sortie du
  script aussi, quoi qu'il arrive ; un drapeau oublié ne compte plus au bout de 15 min.
  Une mesure impossible (jeton absent ou refusé, jauge absente) refuse ;
  `DEPLOY_MALGRE_APPEL=1` passe outre, depuis le poste. Seule une API qui ne répond pas du
  tout (production arrêtée) laisse passer. Les renvois de vidange comptent dans « Appels
  passés en secours », en attention, jamais en panne : sinon la vérification d'après
  lancement défaisait le déploiement. **Le premier déploiement de cette fonction** tourne
  encore contre une version qui ignore le drapeau : il attend la fin des appels sans
  détourner les nouveaux. Un appel accepté juste avant le drapeau n'est compté qu'à
  l'ouverture de son flux : le script attend `VIDANGE_GRACE` (10 s) avant de mesurer.
  **Le retour arrière automatique ne vidange pas** : les appels pris par la nouvelle version
  pendant sa vérification (2 à 3 min) sont coupés si elle est défaite.
- **Les journaux du conteneur** sont copiés dans `/opt/journaux` sur le serveur avant le
  lancement, lisibles par root seul, effacés à 14 jours par la sauvegarde de chaque nuit
  (`scripts/backup-db.sh`, synchronisée au déploiement) et par chaque déploiement
  (`docs/RGPD.md`). Ceux d'une version ratée puis défaite par le retour arrière ne sont pas
  copiés.

**Pas encore branché** (décision du 24/10/2026 : le bouton viendra avec de vrais clients).
Pour le brancher, Helmi :

1. Une clé SSH **dédiée**, sans phrase de passe, qui ne sert qu'à GitHub :
   `ssh-keygen -t ed25519 -N "" -C github-deploiement -f cle-github`, puis ajouter
   `cle-github.pub` à `/root/.ssh/authorized_keys` du serveur. La retirer de ce fichier
   suffit à tout couper.
2. GitHub → Settings → Environments : créer `production` (**Required reviewers** : Helmi ;
   **Deployment branches** : `main` seulement) et `preproduction` (`main` seulement).
   Dans chacun, deux secrets : `DEPLOY_SSH_KEY` (le contenu de `cle-github`) et
   `DEPLOY_KNOWN_HOSTS` (la sortie de `ssh-keyscan -t ed25519 187.77.172.87`, vérifiée contre
   l'empreinte du serveur). Puis effacer `cle-github` du poste.
   **Jamais en secret du dépôt** (Settings → Secrets → Actions), et jamais d'environnement
   sans la règle « `main` seulement » : la clé ouvre root sur le serveur, et c'est cette
   règle seule qui empêche une branche au workflow modifié de s'en servir. La préproduction
   n'a pas d'approbation ; la règle de branche y compte donc autant qu'en production.
3. Préproduction automatique : Settings → Variables → `PREPROD_AUTO` = `1`, une fois la
   préproduction installée (« Préproduction » ci-dessus).

Sans ces secrets, un « Run workflow » échoue tout de suite en le disant ; sans
`PREPROD_AUTO`, rien ne part après une CI.

## Retour arrière (ASSISTANTE-168)

**Automatique.** Après le lancement, `scripts/deploy.sh` vérifie : `/health` répond 200, le
dépôt du serveur est sur le commit, le conteneur tourne sur l'image demandée, le port 8000
est fermé, la sonde `/supervision` répond, et **aucun contrôle n'est passé en panne avec ce
déploiement** (une panne déjà présente avant, comme Twilio suspendu, ne compte pas). Si
l'une de ces vérifications échoue, il remet en service ce qui tournait avant — le commit du
dépôt **et** l'image du conteneur, relevés avant de toucher à quoi que ce soit —, le
revérifie, et sort en erreur en disant ce qui tourne. Le ménage des images n'a lieu
qu'après une vérification réussie : l'image précédente est donc toujours là. Un conteneur
arrêté reste un point de retour ; sans aucun conteneur, le script déploie quand même (pour
pouvoir réparer) et prévient qu'un échec ne sera pas défait.

Ce que la comparaison **ne voit pas**, par construction : une aggravation sur un contrôle
qui était déjà en panne, un contrôle qui passe de « ok » à « attention », et ce qui ne se
voit qu'avec des appels (« Appels muets »). Si la sonde ne répondait pas avant le
déploiement, le script exige seulement qu'elle réponde après, sans comparer les pannes.

**À la main**, vers un commit déjà passé par `main` (refus pour tout autre commit) :

```bash
bash scripts/deploy.sh --version <commit>
```

Mêmes vérifications, même retour arrière s'il échoue. Seuls les commits publiés sur GHCR
(depuis ASSISTANTE-167, 24/10/2026) ont une image : `--version` refuse les autres avant de
toucher à quoi que ce soit. L'image construite sur le serveur avant ce changement est gardée
sous `moshi-rag-voice-assistant-api:avant-ghcr` ; y revenir se fait à la main.

**L'éprouver** : `bash scripts/deploy.sh --simuler-echec` déploie `main`, fait comme si la
vérification échouait, et doit remettre en service la version précédente. Deux
redémarrages du conteneur : hors service, avec l'accord de Helmi.

**La base ne recule pas**, et n'a pas besoin de reculer : les migrations sont en ajout
seulement (vérifié le 24/10/2026 : aucun `DROP TABLE`, `DROP COLUMN` ni `RENAME` dans
`db.py` ; un seul `DROP INDEX`, remplacé par un autre index, sans effet sur le code). Un code plus
ancien sur une base plus récente ne rejoue aucune migration (`_MIGRATIONS[version:]` est
vide) et lit chaque ligne colonne par colonne : une colonne ajoutée ne le gêne pas. Garder
cette règle : une migration qui supprime ou renomme rendrait le retour arrière dangereux.

### Avant l'hébergement

Le PC local + ngrok restent un repli valable : repointer le webhook Twilio sur l'ancienne URL
ngrok suffit à revenir en arrière en < 1 min. Aucun changement n'est destructif — le `Caddyfile`
reste rétro-compatible en local (`SITE_ADDRESS` par défaut `:80`).
