#!/usr/bin/env bash
# Déploie en production — et refuse de le faire si une condition n'est pas tenue.
#
# La règle « la prod, c'est main après CI verte » ne tenait jusqu'ici que par la
# discipline : un ssh + git checkout suffisait à la contourner. C'est exactement ce qui
# a produit un dépôt où la production tournait sur une branche jamais fusionnée.
# Ce script rend la règle mécanique.
#
#   scripts/deploy.sh              déploie main sur le VPS
#   scripts/deploy.sh --check      vérifie les conditions et s'arrête (aucun changement)
#
# Le jeton GitHub vient de ~/.git-credentials (celui dont git se sert déjà pour pousser).
# Il n'est jamais affiché ni copié ailleurs.
set -euo pipefail

BRANCHE_PROD="main"
HOTE="${DEPLOY_HOST:-root@187.77.172.87}"
CLE="${DEPLOY_KEY:-$HOME/.ssh/moshi-vps-deploy}"
CHEMIN="${DEPLOY_PATH:-/opt/moshi-rag-voice-assistant}"
SANTE="${DEPLOY_HEALTH_URL:-https://app.helmane.fr/health}"
DEPOT="helmi75/moshi-rag-voice-assistant"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

refus() { echo "✗ $1" >&2; exit 1; }
etape() { echo "→ $1"; }

# ── 1. Branche et arbre ─────────────────────────────────────────────────────────
branche="$(git rev-parse --abbrev-ref HEAD)"
[ "$branche" = "$BRANCHE_PROD" ] || refus "tu es sur « $branche ». La production se déploie depuis « $BRANCHE_PROD » (git checkout $BRANCHE_PROD)."
git diff --quiet && git diff --cached --quiet || refus "des fichiers suivis sont modifiés — commite ou remise avant de déployer."

# ── 2. Synchronisation avec l'origine ───────────────────────────────────────────
etape "Récupération de l'état distant…"
git fetch -q origin "$BRANCHE_PROD"
local_sha="$(git rev-parse HEAD)"
distant_sha="$(git rev-parse "origin/$BRANCHE_PROD")"
[ "$local_sha" = "$distant_sha" ] || refus "HEAD ($(git rev-parse --short HEAD)) diffère de origin/$BRANCHE_PROD ($(git rev-parse --short origin/$BRANCHE_PROD)) — pousse ou remets-toi à jour d'abord."

# ── 3. CI verte pour CE commit ──────────────────────────────────────────────────
# On interroge le commit exact, pas « la dernière CI de la branche » : c'est la nuance
# qui empêche de déployer un commit poussé pendant qu'une CI plus ancienne était verte.
etape "Vérification de la CI pour ${local_sha:0:8}…"
conclusion="$(
  python3 - "$DEPOT" "$local_sha" <<'PY'
import json, re, sys, urllib.error, urllib.request
from pathlib import Path

depot, sha = sys.argv[1], sys.argv[2]
fichier = Path.home() / ".git-credentials"
jeton = None
if fichier.exists():
    for ligne in fichier.read_text().splitlines():
        m = re.match(r"https://([^:]+):([^@]+)@github\.com", ligne.strip())
        if m:
            jeton = m.group(2)
            break
if not jeton:
    print("SANS_JETON"); sys.exit(0)

req = urllib.request.Request(
    f"https://api.github.com/repos/{depot}/commits/{sha}/check-runs",
    headers={"Authorization": f"Bearer {jeton}", "Accept": "application/vnd.github+json",
             "User-Agent": "helmane-deploy"})
try:
    runs = json.loads(urllib.request.urlopen(req, timeout=30).read())["check_runs"]
except urllib.error.HTTPError as e:
    print(f"ERREUR_HTTP_{e.code}"); sys.exit(0)

if not runs:
    print("AUCUN_RUN")
elif any(r["status"] != "completed" for r in runs):
    print("EN_COURS")
elif all(r["conclusion"] == "success" for r in runs):
    print("VERTE")
else:
    echecs = [r["name"] for r in runs if r["conclusion"] != "success"]
    print("ROUGE:" + ",".join(echecs))
PY
)"
case "$conclusion" in
  VERTE)      etape "CI verte." ;;
  EN_COURS)   refus "la CI n'a pas fini sur ce commit — attends-la." ;;
  AUCUN_RUN)  refus "aucune CI trouvée pour ce commit. Pousse-le et attends la CI." ;;
  ROUGE:*)    refus "CI en échec sur ce commit (${conclusion#ROUGE:})." ;;
  SANS_JETON) refus "aucun jeton GitHub dans ~/.git-credentials : impossible de vérifier la CI." ;;
  *)          refus "état de CI indéterminé ($conclusion) — je ne déploie pas à l'aveugle." ;;
esac

if [ "$CHECK_ONLY" = 1 ]; then
  echo "✓ Toutes les conditions sont réunies pour déployer ${local_sha:0:8}."
  exit 0
fi

# ── 4. Déploiement ──────────────────────────────────────────────────────────────
# reset --hard sur le SHA EXACT, jamais `git pull` : pull suivrait une branche, et c'est
# précisément ce qui laisse une machine dériver de ce qu'on croit avoir déployé.
etape "Déploiement de ${local_sha:0:8} sur $HOTE…"
# Le rechargement de Caddy est conditionnel et VALIDÉ d'abord : `docker compose up -d`
# ne recrée pas le conteneur quand seul le contenu du Caddyfile change, donc un
# changement de configuration du proxy serait déployé sur le disque sans jamais être
# appliqué — et le script annoncerait « déployé et vérifié ».
ssh -i "$CLE" -o ConnectTimeout=20 "$HOTE" "
  set -e
  cd '$CHEMIN'
  avant=\$(sha256sum caddy/Caddyfile | cut -d' ' -f1)
  git fetch -q origin '$BRANCHE_PROD'
  git checkout -q '$BRANCHE_PROD'
  git reset -q --hard '$local_sha'
  apres=\$(sha256sum caddy/Caddyfile | cut -d' ' -f1)
  docker compose up -d --build 2>&1 | tail -3
  if [ \"\$avant\" != \"\$apres\" ]; then
    echo '   Caddyfile modifié : validation puis rechargement'
    docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
    docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1
  fi
"

# ── 5. Vérification ─────────────────────────────────────────────────────────────
etape "Attente de la remise en route…"
for i in $(seq 1 24); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$SANTE" || true)"
  [ "$code" = "200" ] && break
  sleep 5
done
[ "${code:-}" = "200" ] || refus "$SANTE ne répond pas 200 après le déploiement (dernier code : ${code:-aucun})."

deploye="$(ssh -i "$CLE" -o ConnectTimeout=20 "$HOTE" "git -C '$CHEMIN' rev-parse HEAD")"
[ "$deploye" = "$local_sha" ] || refus "le VPS est sur ${deploye:0:8}, pas ${local_sha:0:8}."

# L'API ne doit JAMAIS être joignable hors Caddy. Vérifié depuis ici, pas depuis le
# serveur : ce qui compte est ce qu'un tiers voit. Un `git pull` a déjà rouvert ce port
# deux fois, découvert chaque fois par hasard.
etape "Contrôle du port 8000 depuis l'extérieur…"
hote_nu="${HOTE#*@}"
if curl -sI --max-time 8 "http://${hote_nu}:8000/" >/dev/null 2>&1; then
  refus "l'API répond en HTTP nu sur ${hote_nu}:8000 — hors TLS et hors Caddy."
fi
etape "Port 8000 fermé."

# Le script de sauvegarde vit HORS du dépôt sur le serveur : cron l'appelle depuis
# /opt/backups, et `install` en avait fait une copie figée. Rien ne la met donc à jour —
# même piège que le Caddyfile le 23/08 : un fichier déposé n'est pas un fichier appliqué.
# Constaté : la copie installée avait déjà divergé de celle du dépôt.
etape "Synchronisation du script de sauvegarde…"
ssh -i "$CLE" -o ConnectTimeout=20 "$HOTE" "
  set -e
  depot='$CHEMIN/scripts/backup-db.sh'
  installe=/opt/backups/backup-db.sh
  if [ ! -f \"\$depot\" ]; then
    echo '  ⚠ scripts/backup-db.sh absent du dépôt déployé.'
  elif cmp -s \"\$depot\" \"\$installe\"; then
    echo '  déjà à jour.'
  else
    install -m 755 \"\$depot\" \"\$installe\"
    echo '  mis à jour depuis le dépôt (le cron utilisera la nouvelle version).'
  fi
"

# Deux questions distinctes, et il faut les traiter différemment.
#
# 1) La supervision FONCTIONNE-t-elle ? Si la sonde ne répond pas, ou répond 401/404,
#    la production tourne sans surveillance — c'est un défaut du déploiement lui-même,
#    donc un REFUS. Vécu le 24/08 : le jeton était dans le `.env` du VPS mais absent de
#    `docker-compose.yml`, la sonde renvoyait 404, et le déploiement s'était déclaré
#    « vérifié ». Exactement le piège des en-têtes Caddy du 23/08.
#
# 2) La pile est-elle EN BONNE SANTÉ ? Là, simple rapport : une sauvegarde en retard
#    n'a pas à empêcher de livrer un correctif, et une porte qui se ferme pour des
#    motifs sans rapport finit par être contournée.
#
# Le jeton est lu sur le VPS et n'est jamais affiché.
etape "Verdict de la supervision…"
jeton="$(ssh -i "$CLE" -o ConnectTimeout=20 "$HOTE" \
  "grep -m1 '^SUPERVISION_TOKEN=' '$CHEMIN/.env' 2>/dev/null | cut -d= -f2-" || true)"
if [ -z "${jeton:-}" ]; then
  refus "SUPERVISION_TOKEN absent du .env du serveur : la production tournerait sans surveillance (cf. docs/SUPERVISION.md)."
fi
sonde="${SANTE%/health}/supervision"
reponse="$(mktemp)"
code_sonde="$(curl -s --max-time 25 -o "$reponse" -w '%{http_code}' \
  -H "X-Supervision-Token: $jeton" "$sonde" || echo 000)"
case "$code_sonde" in
  200|503) : ;;
  404) rm -f "$reponse"; refus "la sonde répond 404 : SUPERVISION_TOKEN n'atteint pas le conteneur (est-il bien listé dans docker-compose.yml ?)." ;;
  401) rm -f "$reponse"; refus "la sonde refuse le jeton du .env du serveur (401) — jeton désynchronisé." ;;
  *)   rm -f "$reponse"; refus "la sonde ne répond pas (HTTP $code_sonde) : la production tournerait sans surveillance." ;;
esac
verdict="$(python3 -c 'import json,sys; e=json.load(open(sys.argv[1])); print(e["niveau"], "|", ", ".join(c["titre"]+" : "+c["resume"] for c in e["controles"] if c["niveau"]!="ok") or "tous les contrôles au vert")' "$reponse" 2>/dev/null || echo "réponse illisible")"
rm -f "$reponse"
echo "  $verdict"

echo "✓ ${local_sha:0:8} déployé et vérifié — $SANTE répond 200."
