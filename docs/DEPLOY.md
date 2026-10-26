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
