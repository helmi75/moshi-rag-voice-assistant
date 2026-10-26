#!/usr/bin/env bash
# Déploie en préproduction (ASSISTANTE-174) : la même image que la production, sur le même
# serveur, avant elle. Elle sert à voir une version démarrer, migrer sa base et servir ses
# pages sans toucher aux appels : elle n'a ni numéro ni clé de fournisseur (décision du
# 26/10/2026), donc elle ne dépense rien.
#
#   scripts/deploy_preprod.sh              déploie le dernier commit de main
#   scripts/deploy_preprod.sh <commit>     déploie un commit déjà passé par main
#   scripts/deploy_preprod.sh --installer  prépare la préproduction sur le serveur (une fois)
#
# Pas de retour arrière : une préproduction qui ne démarre pas est précisément ce qu'on
# voulait apprendre avant la production. Elle reste en l'état pour qu'on la regarde.
#
# Elle vit dans sa propre copie du dépôt (/opt/helmane-preprod), avec sa base (volume
# helmane-preprod_api_data) et son `.env` ; la production n'est jamais touchée, Caddy
# compris (son bloc vient de la copie de la production : caddy/Caddyfile).
set -euo pipefail

HOTE="${DEPLOY_HOST:-root@187.77.172.87}"
CLE="${DEPLOY_KEY:-$HOME/.ssh/moshi-vps-deploy}"
CHEMIN="${PREPROD_PATH:-/opt/helmane-preprod}"
URL="${PREPROD_URL:-https://preprod.helmane.fr}"
DEPOT_GIT="https://github.com/helmi75/moshi-rag-voice-assistant.git"
REGISTRE="ghcr.io/helmi75/moshi-rag-voice-assistant"
IMAGE_LOCALE="moshi-rag-voice-assistant-api:preprod"
COMPOSE="docker compose -f docker-compose.preprod.yml"
# Même règle que scripts/deploy.sh : les cinq images les plus récentes restent sur le disque.
GARDER=5
LABEL_REVISION='{{ index .Config.Labels "org.opencontainers.image.revision" }}'

refus() { echo "✗ $1" >&2; exit 1; }
etape() { echo "→ $1"; }
distant() { ssh -i "$CLE" -o ConnectTimeout=20 "$HOTE" "$@"; }

# ── Installation (une fois) ─────────────────────────────────────────────────────
# Les secrets naissent sur le serveur et n'en sortent pas. Le mot de passe de l'admin
# n'est pas tiré au sort : Helmi le pose lui-même dans le `.env` (sans lui, l'admin de la
# préproduction reste fermée, le reste démarre).
if [ "${1:-}" = "--installer" ]; then
  distant "
    set -eo pipefail
    if [ -e '$CHEMIN/.env' ]; then echo '  $CHEMIN/.env existe déjà : rien n’est remplacé.'; exit 0; fi
    [ -d '$CHEMIN/.git' ] || git clone -q '$DEPOT_GIT' '$CHEMIN'
    umask 077
    cat > '$CHEMIN/.env' <<FIN
# Préproduction (ASSISTANTE-174) : aucun numéro, aucune clé de fournisseur, aucun e-mail.
PUBLIC_URL=$URL
# Sans jeton Twilio, « enforce » refuse tout webhook : personne ne fait parler la
# préproduction en se faisant passer pour Twilio.
TWILIO_SIGNATURE=enforce
SESSION_SECURE=1
SESSION_SECRET=\$(openssl rand -hex 32)
SUPERVISION_TOKEN=\$(openssl rand -hex 32)
SUPERVISION_TOURNEE_HEURE=non
SUPERVISION_TWILIO_SECONDES=0
RAPPEL_ACTIF=0
ADMIN_EMAIL=admin@preprod.helmane.fr
# ADMIN_PASSWORD=   à poser à la main pour ouvrir l'admin de la préproduction
FIN
    echo '  $CHEMIN prêt ; .env créé (secrets tirés sur le serveur, jamais affichés).'
  "
  exit 0
fi

# ── 1. Le commit : un commit de main, CI verte ──────────────────────────────────
git fetch -q origin main
demande="${1:-origin/main}"
case "$demande" in -*) refus "option inconnue : $demande" ;; esac
sha="$(git rev-parse --verify -q "$demande^{commit}")" || refus "commit inconnu : $demande (git fetch ?)."
git merge-base --is-ancestor "$sha" origin/main \
  || refus "${sha:0:8} n'est pas un commit de main : la préproduction ne sert que ce qui y est passé."

etape "Vérification de la CI pour ${sha:0:8}…"
code=0
python3 "$(dirname "$0")/depot_github.py" ci "$sha" >/dev/null || code=$?
case "$code" in
  0) etape "CI verte." ;;
  2) refus "la CI n'a pas fini sur ce commit — attends-la." ;;
  *) refus "CI en échec ou illisible sur ce commit (python3 scripts/depot_github.py ci ${sha:0:8})." ;;
esac

distant "test -f '$CHEMIN/.env'" \
  || refus "préproduction non installée sur le serveur : scripts/deploy_preprod.sh --installer (docs/DEPLOY.md, « Préproduction »)."

# ── 2. L'image du commit ────────────────────────────────────────────────────────
image="$REGISTRE:sha-$sha"
etape "Téléchargement de l'image ${sha:0:8}…"
distant "docker pull -q '$image' >/dev/null" \
  || refus "le serveur n'a pas pu télécharger $image (job « image » de la CI ?)."
revision="$(distant "docker inspect -f '$LABEL_REVISION' '$image'" 2>/dev/null || echo illisible)"
[ "$revision" = "$sha" ] || refus "l'image $image porte la révision « ${revision:-aucune} », pas $sha."
image_id="$(distant "docker image inspect -f '{{.Id}}' '$image'")"

# ── 3. Lancer ───────────────────────────────────────────────────────────────────
# reset --hard sur le SHA exact, comme la production : la configuration (docker-compose.yml)
# est celle du commit servi, jamais celle d'une branche qui aurait bougé.
etape "Déploiement de ${sha:0:8} en préproduction…"
distant "
  set -eo pipefail
  cd '$CHEMIN'
  git cat-file -e '$sha^{commit}' 2>/dev/null || git fetch -q origin main
  git checkout -q --detach '$sha'
  git reset -q --hard '$sha'
  docker tag '$image' '$IMAGE_LOCALE'
  $COMPOSE up -d --no-build 2>&1 | tail -3
"

# ── 4. Vérifier ─────────────────────────────────────────────────────────────────
etape "Attente de la remise en route…"
code=""
for _ in $(seq 1 24); do
  code="$(distant "curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8001/health" 2>/dev/null || true)"
  [ "$code" = "200" ] && break
  sleep 5
done
[ "$code" = "200" ] || refus "la préproduction ne répond pas 200 sur le serveur (dernier code : ${code:-aucun}) : $COMPOSE logs api_preprod, dans $CHEMIN."

conteneur="$(distant "cd '$CHEMIN' && docker inspect -f '{{.Image}}' \$($COMPOSE ps -q api_preprod)" 2>/dev/null || echo illisible)"
[ "$conteneur" = "$image_id" ] || refus "le conteneur tourne sur l'image ${conteneur:7:12}, pas ${image_id:7:12}."
etape "Le conteneur tourne sur l'image attendue (${sha:0:8})."

# Ce qu'un tiers voit, vérifié depuis ici : le port de la préproduction reste fermé.
hote_nu="${HOTE#*@}"
if curl -sI --max-time 8 "http://${hote_nu}:8001/" >/dev/null 2>&1; then
  refus "la préproduction répond en HTTP nu sur ${hote_nu}:8001 — hors TLS et hors Caddy."
fi
etape "Port 8001 fermé."

# La sonde doit répondre (200, ou 503 : sans numéro ni clé, des contrôles sont en panne,
# c'est attendu). Une sonde muette dirait que la supervision elle-même ne démarre pas.
sonde="$(distant "jeton=\$(grep -m1 '^SUPERVISION_TOKEN=' '$CHEMIN/.env' | cut -d= -f2-); curl -s -o /dev/null -w '%{http_code}' --max-time 25 -H \"X-Supervision-Token: \$jeton\" http://127.0.0.1:8001/supervision" 2>/dev/null || echo 000)"
case "$sonde" in
  200|503) etape "Sonde de supervision : HTTP $sonde." ;;
  *)       refus "la sonde de la préproduction répond HTTP $sonde." ;;
esac

# Ménage : comme scripts/deploy.sh. `latest` (production) et `preprod` gardent leur image.
distant "
  docker images '$REGISTRE' --format '{{.Tag}}' | grep '^sha-' | tail -n +$((GARDER + 1)) \
    | while read -r t; do docker rmi -f '$REGISTRE':\$t >/dev/null 2>&1 || true; done
"

public="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$URL/health" || true)"
[ "$public" = "200" ] \
  || refus "${sha:0:8} tourne en préproduction, mais $URL/health répond « ${public:-rien} » : DNS, ou PREPROD_ADDRESS absent du .env de la production (docs/DEPLOY.md, « Préproduction »)."
echo "✓ ${sha:0:8} déployé en préproduction et vérifié — $URL répond 200."
