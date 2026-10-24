#!/usr/bin/env bash
# Déploie en production — et refuse de le faire si une condition n'est pas tenue.
#
# La règle « la prod, c'est main après CI verte » ne tenait jusqu'ici que par la
# discipline : un ssh + git checkout suffisait à la contourner. C'est exactement ce qui
# a produit un dépôt où la production tournait sur une branche jamais fusionnée.
# Ce script rend la règle mécanique.
#
#   scripts/deploy.sh                    déploie main sur le VPS
#   scripts/deploy.sh --check            vérifie les conditions et s'arrête (aucun changement)
#   scripts/deploy.sh --version <commit> remet en service un commit déjà passé par main
#   scripts/deploy.sh --simuler-echec    déploie main, puis fait comme si la vérification
#                                        échouait : éprouve le retour arrière
#
# Si la vérification échoue après le lancement (santé, révision du conteneur, port 8000,
# sonde de supervision muette, ou un contrôle qui PASSE en panne), le script remet en
# service ce qui tournait avant — le code et l'image — et le dit (ASSISTANTE-168).
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
# L'image est construite par la CI depuis le commit testé et publiée sur GHCR
# (ASSISTANTE-167). Le serveur la télécharge par l'empreinte du commit et lui donne le nom
# que docker-compose.yml lance : il ne construit plus rien.
REGISTRE="ghcr.io/helmi75/moshi-rag-voice-assistant"
IMAGE_LOCALE="moshi-rag-voice-assistant-api:latest"
# Images gardées sur le serveur, la courante comprise : de quoi revenir en arrière sans
# dépendre du registre. ~1,2 Go chacune ; 91 Go libres relevés le 24/10/2026.
GARDER=5
# Le gabarit de `docker inspect` qui lit la révision d'une image. Des guillemets doubles
# simples, sans barre oblique : il est inséré entre apostrophes dans les commandes distantes.
LABEL_REVISION='{{ index .Config.Labels "org.opencontainers.image.revision" }}'

CHECK_ONLY=0
SIMULER_ECHEC=0
VERSION=""
case "${1:-}" in
  "")              ;;
  --check)         CHECK_ONLY=1 ;;
  --simuler-echec) SIMULER_ECHEC=1 ;;
  --version)       VERSION="${2:-}"; [ -n "$VERSION" ] || { echo "✗ --version attend un commit" >&2; exit 1; } ;;
  *)               echo "✗ option inconnue : $1" >&2; exit 1 ;;
esac

refus() { echo "✗ $1" >&2; exit 1; }
etape() { echo "→ $1"; }
distant() { ssh -i "$CLE" -o ConnectTimeout=20 "$HOTE" "$@"; }

# ── 1. Branche, arbre, origine ──────────────────────────────────────────────────
etape "Récupération de l'état distant…"
git fetch -q origin "$BRANCHE_PROD"
if [ -z "$VERSION" ]; then
  branche="$(git rev-parse --abbrev-ref HEAD)"
  [ "$branche" = "$BRANCHE_PROD" ] || refus "tu es sur « $branche ». La production se déploie depuis « $BRANCHE_PROD » (git checkout $BRANCHE_PROD)."
  git diff --quiet && git diff --cached --quiet || refus "des fichiers suivis sont modifiés — commite ou remise avant de déployer."
  local_sha="$(git rev-parse HEAD)"
  distant_sha="$(git rev-parse "origin/$BRANCHE_PROD")"
  [ "$local_sha" = "$distant_sha" ] || refus "HEAD ($(git rev-parse --short HEAD)) diffère de origin/$BRANCHE_PROD ($(git rev-parse --short origin/$BRANCHE_PROD)) — pousse ou remets-toi à jour d'abord."
else
  # Revenir à une version donnée : seulement un commit qui est passé par main, jamais
  # une branche de travail.
  local_sha="$(git rev-parse --verify -q "$VERSION^{commit}")" || refus "commit inconnu : $VERSION (git fetch ?)."
  git merge-base --is-ancestor "$local_sha" "origin/$BRANCHE_PROD" \
    || refus "${local_sha:0:8} n'est pas un commit de $BRANCHE_PROD : on ne remet en service que ce qui y est passé."
fi

# ── 2. CI verte pour CE commit ──────────────────────────────────────────────────
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

# Seulement les tâches de la CI, demandées par leur nom. La sonde de supervision
# (workflow planifié) rattache ses exécutions au dernier commit de main : le 24/10/2026,
# une production en panne (Twilio suspendu) a mis une sonde rouge sur 7576c40 et fait
# refuser son redéploiement pour « CI en échec ». Et l'API ne rend que les 30 premières
# vérifications d'un commit : les passages de la sonde finiraient par cacher la CI.
# `image` est exigée : c'est elle qui publie ce que le serveur va lancer.
def lire(nom):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{depot}/commits/{sha}/check-runs?check_name={nom}&per_page=100",
        headers={"Authorization": f"Bearer {jeton}", "Accept": "application/vnd.github+json",
                 "User-Agent": "helmane-deploy"})
    # Seulement ce que publie GitHub Actions : une autre application qui nommerait une
    # vérification `tests` ne doit pas compter comme notre CI.
    return [r for r in json.loads(urllib.request.urlopen(req, timeout=30).read())["check_runs"]
            if (r.get("app") or {}).get("slug") == "github-actions"]

try:
    par_tache = {nom: lire(nom) for nom in ("tests", "image")}
except urllib.error.HTTPError as e:
    print(f"ERREUR_HTTP_{e.code}"); sys.exit(0)
except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
    print(f"ERREUR_RESEAU_{type(e).__name__}"); sys.exit(0)

# Un même commit peut avoir plusieurs exécutions (poussée, PR) : sur une PR, `image` est
# sautée. Il en faut une réussie par tâche, et aucune en échec ni en cours.
runs = [r for liste in par_tache.values() for r in liste if r["conclusion"] != "skipped"]
if not all(par_tache.values()):
    manquent = [nom for nom, liste in par_tache.items() if not liste]
    print("AUCUN_RUN" if len(manquent) == 2 else "ROUGE:" + ",".join(f"{n} absente" for n in manquent))
elif any(r["status"] != "completed" for r in runs):
    print("EN_COURS")
elif any(r["conclusion"] != "success" for r in runs) or \
        any(not any(r["conclusion"] == "success" for r in liste) for liste in par_tache.values()):
    echecs = sorted({r["name"] for r in runs if r["conclusion"] != "success"}
                    | {n for n, liste in par_tache.items() if not any(r["conclusion"] == "success" for r in liste)})
    print("ROUGE:" + ",".join(echecs))
else:
    print("VERTE")
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

# ── 3. Ce qui tourne avant : le point de retour ─────────────────────────────────
# Le commit du dépôt du serveur et l'IMAGE du conteneur (son identifiant, pas une
# étiquette : `latest` va bouger). Le ménage n'a lieu qu'après une vérification réussie,
# donc cette image est toujours là si le retour arrière en a besoin.
precedent_sha="$(distant "git -C '$CHEMIN' rev-parse HEAD")"
# `ps -a` : un conteneur arrêté a toujours son image. Production tombée = le moment où il
# faut pouvoir livrer un correctif ; sans aucun conteneur, on déploie sans point de retour,
# et on le dit, plutôt que de bloquer la réparation.
precedent_image="$(distant "cd '$CHEMIN' && docker inspect -f '{{.Image}}' \$(docker compose ps -a -q api)" 2>/dev/null || true)"
[ -n "$precedent_image" ] || echo "⚠ aucun conteneur api sur le serveur : pas de point de retour, un échec ne pourra pas être défait."

# La sonde AVANT : un contrôle déjà en panne (Twilio suspendu, par exemple) ne doit pas
# faire annuler un déploiement qui n'y est pour rien. Le jeton est lu sur le serveur et
# n'est jamais affiché.
jeton="$(distant "grep -m1 '^SUPERVISION_TOKEN=' '$CHEMIN/.env' 2>/dev/null | cut -d= -f2-" || true)"
[ -n "${jeton:-}" ] || refus "SUPERVISION_TOKEN absent du .env du serveur : la production tournerait sans surveillance (cf. docs/SUPERVISION.md)."
SONDE="${SANTE%/health}/supervision"
REPONSE="$(mktemp)"
trap 'rm -f "$REPONSE"' EXIT

# Interroge la sonde ; rend le code HTTP, et laisse le corps dans $REPONSE.
sonder() {
  curl -s --max-time 25 -o "$REPONSE" -w '%{http_code}' -H "X-Supervision-Token: $jeton" "$SONDE" || echo 000
}
# Les clés des contrôles en panne, triées, une par ligne.
pannes() {
  python3 -c 'import json,sys; print("\n".join(sorted(c["cle"] for c in json.load(open(sys.argv[1]))["controles"] if c["niveau"]=="panne")))' "$REPONSE" 2>/dev/null || echo "illisible"
}
code_avant="$(sonder)"
case "$code_avant" in
  200|503) pannes_avant="$(pannes)" ;;
  *)       pannes_avant="inconnues" ;;   # la sonde muette AVANT : on exigera seulement qu'elle réponde après
esac

# ── 4. L'image du commit ────────────────────────────────────────────────────────
# Téléchargée avant de toucher à quoi que ce soit : si le registre la refuse (serveur non
# connecté à GHCR, image pas encore publiée), rien n'a bougé.
image="$REGISTRE:sha-$local_sha"
etape "Téléchargement de l'image ${local_sha:0:8} sur $HOTE…"
distant "docker pull -q '$image' >/dev/null" \
  || refus "le serveur n'a pas pu télécharger $image. Image pas encore publiée (job « image » de la CI), ou serveur non connecté à GHCR (docs/DEPLOY.md, « Image »)."
revision="$(distant "docker inspect -f '$LABEL_REVISION' '$image'" 2>/dev/null || echo "illisible")"
[ "$revision" = "$local_sha" ] || refus "l'image $image porte la révision « ${revision:-aucune} », pas $local_sha."

# ── 5. Lancer une version : le code ET l'image ──────────────────────────────────
# Sert au déploiement comme au retour arrière. reset --hard sur le SHA EXACT, jamais
# `git pull` : pull suivrait une branche, et c'est précisément ce qui laisse une machine
# dériver de ce qu'on croit avoir déployé. Le rechargement de Caddy est conditionnel et
# VALIDÉ d'abord : `docker compose up -d` ne recrée pas le conteneur quand seul le
# Caddyfile change, donc un changement du proxy serait sur le disque sans être appliqué.
lancer() {
  local sha="$1" image_ref="$2"
  distant "
    set -eo pipefail
    cd '$CHEMIN'
    avant=\$(sha256sum caddy/Caddyfile | cut -d' ' -f1)
    # Le réseau seulement si le commit manque : un retour arrière vise un commit déjà là,
    # et ne doit pas échouer parce que GitHub ne répond pas.
    git cat-file -e '$sha^{commit}' 2>/dev/null || git fetch -q origin '$BRANCHE_PROD'
    git checkout -q '$BRANCHE_PROD'
    git reset -q --hard '$sha'
    apres=\$(sha256sum caddy/Caddyfile | cut -d' ' -f1)
    # L'image d'avant le passage au registre (construite sur le serveur, sans révision)
    # garde un nom : sans lui, elle deviendrait orpheline, alors qu'elle est le seul
    # retour possible vers un commit antérieur à ASSISTANTE-167.
    if docker image inspect '$IMAGE_LOCALE' >/dev/null 2>&1 && [ -z \"\$(docker image inspect -f '$LABEL_REVISION' '$IMAGE_LOCALE')\" ]; then
      docker tag '$IMAGE_LOCALE' moshi-rag-voice-assistant-api:avant-ghcr
    fi
    docker tag '$image_ref' '$IMAGE_LOCALE'
    docker compose up -d --no-build 2>&1 | tail -3
    if [ \"\$avant\" != \"\$apres\" ]; then
      echo '   Caddyfile modifié : validation puis rechargement'
      docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
      docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1
    fi
  "
}

# ── 6. Vérifier : rend 0, ou écrit la raison de l'échec dans $ECHEC ─────────────
ECHEC=""
verifier() {
  local sha="$1" image_id="$2" code="" i
  ECHEC=""
  etape "Attente de la remise en route…"
  for i in $(seq 1 24); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$SANTE" || true)"
    [ "$code" = "200" ] && break
    sleep 5
  done
  [ "$code" = "200" ] || { ECHEC="$SANTE ne répond pas 200 (dernier code : ${code:-aucun})"; return 1; }

  local depot conteneur
  depot="$(distant "git -C '$CHEMIN' rev-parse HEAD" 2>/dev/null || echo illisible)"
  [ "$depot" = "$sha" ] || { ECHEC="le dépôt du serveur est sur ${depot:0:8}, pas ${sha:0:8}"; return 1; }
  # Le conteneur qui tourne, pas seulement le dépôt : son image doit être celle demandée.
  conteneur="$(distant "cd '$CHEMIN' && docker inspect -f '{{.Image}}' \$(docker compose ps -q api)" 2>/dev/null || echo illisible)"
  [ "$conteneur" = "$image_id" ] || { ECHEC="le conteneur tourne sur l'image ${conteneur:7:12}, pas ${image_id:7:12}"; return 1; }
  etape "Le conteneur tourne sur l'image attendue (${sha:0:8})."

  # L'API ne doit JAMAIS être joignable hors Caddy. Vérifié depuis ici, pas depuis le
  # serveur : ce qui compte est ce qu'un tiers voit. Un `git pull` a déjà rouvert ce port
  # deux fois, découvert chaque fois par hasard.
  etape "Contrôle du port 8000 depuis l'extérieur…"
  local hote_nu="${HOTE#*@}"
  if curl -sI --max-time 8 "http://${hote_nu}:8000/" >/dev/null 2>&1; then
    ECHEC="l'API répond en HTTP nu sur ${hote_nu}:8000 — hors TLS et hors Caddy"; return 1
  fi
  etape "Port 8000 fermé."

  # La supervision FONCTIONNE-t-elle ? Une sonde muette, 401 ou 404, c'est une production
  # sans surveillance : un défaut du déploiement lui-même. Vécu le 24/08 : le jeton était
  # dans le `.env` mais absent de docker-compose.yml, la sonde renvoyait 404, et le
  # déploiement s'était déclaré « vérifié ». Puis : un contrôle qui PASSE en panne avec ce
  # déploiement l'annule ; une panne qui était déjà là ne compte pas — sinon une
  # sauvegarde en retard bloquerait un correctif sans rapport, et la porte finirait
  # contournée. Trois lectures espacées : un contrôle peut sortir de panne juste après le
  # démarrage.
  etape "Supervision…"
  local code_sonde nouvelles=""
  for i in 1 2 3; do
    code_sonde="$(sonder)"
    case "$code_sonde" in
      200|503) ;;
      404) ECHEC="la sonde répond 404 : SUPERVISION_TOKEN n'atteint pas le conteneur (docker-compose.yml ?)"; return 1 ;;
      401) ECHEC="la sonde refuse le jeton du .env du serveur (401)"; return 1 ;;
      *)   ECHEC="la sonde ne répond pas (HTTP $code_sonde) : la production tournerait sans surveillance"; return 1 ;;
    esac
    [ "$pannes_avant" = "inconnues" ] && break
    nouvelles="$(comm -13 <(printf '%s\n' "$pannes_avant") <(pannes) | sed '/^$/d' | paste -sd, -)"
    [ -z "$nouvelles" ] && break
    sleep 16   # la sonde garde son verdict 15 s en cache
  done
  [ -z "$nouvelles" ] || { ECHEC="contrôle(s) passé(s) en panne avec ce déploiement : $nouvelles"; return 1; }
  python3 -c 'import json,sys; e=json.load(open(sys.argv[1])); print("  ", e["niveau"], "|", ", ".join(c["titre"]+" : "+c["resume"] for c in e["controles"] if c["niveau"]!="ok") or "tous les contrôles au vert")' "$REPONSE" 2>/dev/null || echo "   réponse illisible"

  if [ "$SIMULER_ECHEC" = 1 ]; then
    ECHEC="échec simulé (--simuler-echec), pour éprouver le retour arrière"; return 1
  fi
  return 0
}

# ── 7. Déployer, vérifier, et revenir en arrière si besoin ──────────────────────
image_id="$(distant "docker image inspect -f '{{.Id}}' '$image'")"
etape "Déploiement de ${local_sha:0:8} sur $HOTE (en service : ${precedent_sha:0:8})…"
lancer "$local_sha" "$image" || ECHEC="le lancement a échoué sur le serveur"
if [ -n "$ECHEC" ] || ! verifier "$local_sha" "$image_id"; then
  echo "✗ Déploiement de ${local_sha:0:8} refusé : $ECHEC." >&2
  [ -n "$precedent_image" ] || refus "aucun point de retour (pas de conteneur avant le déploiement) : ${local_sha:0:8} reste en place, à réparer à la main."
  etape "Retour arrière : remise en service de ${precedent_sha:0:8}…"
  SIMULER_ECHEC=0
  lancer "$precedent_sha" "$precedent_image" || refus "RETOUR ARRIÈRE IMPOSSIBLE : le lancement de ${precedent_sha:0:8} a échoué sur le serveur. La production est peut-être arrêtée : intervenir à la main."
  # La version d'avant peut rater une vérification qu'elle ratait déjà (port, sonde) :
  # on le dit sans crier à l'incident.
  verifier "$precedent_sha" "$precedent_image" \
    || refus "${local_sha:0:8} n'est PAS déployé ; ${precedent_sha:0:8} est relancé, mais : $ECHEC. Regarder si c'était déjà le cas avant ce déploiement."
  refus "${local_sha:0:8} n'est PAS déployé ; ${precedent_sha:0:8} est de nouveau en service et vérifié."
fi

# Les images plus anciennes que les $GARDER dernières partent : sans ce ménage, chaque
# déploiement laisserait 1,2 Go de plus sur le disque. Ordre : date de construction par la
# CI. Une image dont la construction est entièrement en cache est la même image (même
# empreinte), donc l'ordre ne la trompe pas. Jamais celle qui tourne : elle porte `latest`
# et sert au conteneur, `rmi` ne lui retire qu'une étiquette. La précédente peut partir si
# elle est plus vieille que les $GARDER dernières (après un `--version` lointain) : elle
# reste au registre, `--version` la retélécharge.
distant "
  docker images '$REGISTRE' --format '{{.Tag}}' | grep '^sha-' | tail -n +$((GARDER + 1)) \
    | while read -r t; do docker rmi -f '$REGISTRE':\$t >/dev/null 2>&1 || true; done
"

# Le script de sauvegarde vit HORS du dépôt sur le serveur : cron l'appelle depuis
# /opt/backups, et `install` en avait fait une copie figée. Rien ne la met donc à jour —
# même piège que le Caddyfile le 23/08 : un fichier déposé n'est pas un fichier appliqué.
# Constaté : la copie installée avait déjà divergé de celle du dépôt.
etape "Synchronisation du script de sauvegarde…"
distant "
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

echo "✓ ${local_sha:0:8} déployé et vérifié — $SANTE répond 200."
