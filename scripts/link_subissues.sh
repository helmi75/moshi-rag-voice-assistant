#!/usr/bin/env bash
#
# Rattache chaque ticket à son epic en SOUS-ISSUE GitHub (hiérarchie native).
#
# Pourquoi ce script : les epics #11 à #19 listaient leurs tickets en simple
# liste de tâches Markdown. Ça ressemble à une hiérarchie mais ça n'en est pas
# une : les cases ne se cochent pas quand le ticket se ferme, et l'epic n'affiche
# aucune barre « X/X ». Seules les vraies sous-issues font ça. L'API sous-issues
# renvoie ~8 Ko par appel, ce qui rend l'opération coûteuse depuis un agent ;
# en local avec `gh`, les 51 liens passent en quelques secondes.
#
# Prérequis : gh CLI authentifié (`gh auth status`).
# Usage     : bash scripts/link_subissues.sh
#
# Idempotent : un lien déjà posé renvoie une erreur que l'on ignore, on peut
# donc relancer le script sans rien casser.

set -uo pipefail

REPO="${REPO:-helmi75/moshi-rag-voice-assistant}"

# epic:ticket ticket ticket...
# Reflète le découpage du backlog (voir docs/backlog-jira.csv).
MAPPING=(
  "10:42 43 44 45 46 47 48"                        # Socle conversationnel
  "11:28 49 50 51 52 53 54 55 56 57 58"            # Voix temps réel Moshi
  "12:41 59 60 61 62 63 64 65 66 67"               # Latence et naturalité
  "13:68 69 70 71"                                 # Plateforme admin
  "14:23 24 27 72 73 74"                           # Production et hébergement
  "15:75 76"                                       # Qualité et CI
  "16:25 26 39 40"                                 # Montée en charge et coûts
  "17:20 21 22"                                    # Sécurité et conformité
  "18:29 30 31 32 77 78"                           # Offre commerciale
  "19:33 34 35 36 37 38"                           # Couche SaaS
)

# L'API sous-issues attend l'ID interne du ticket, pas son numéro.
issue_id() {
  gh api "repos/$REPO/issues/$1" --jq .id 2>/dev/null
}

poses=0
deja=0
echecs=0

for entree in "${MAPPING[@]}"; do
  epic="${entree%%:*}"
  enfants="${entree#*:}"
  printf '\nEpic #%s\n' "$epic"

  for enfant in $enfants; do
    id="$(issue_id "$enfant")"
    if [ -z "$id" ]; then
      printf '  #%-3s introuvable\n' "$enfant"
      echecs=$((echecs + 1))
      continue
    fi

    erreur="$(gh api -X POST "repos/$REPO/issues/$epic/sub_issues" \
                -F "sub_issue_id=$id" 2>&1 >/dev/null)"

    if [ -z "$erreur" ]; then
      printf '  #%-3s rattaché\n' "$enfant"
      poses=$((poses + 1))
    elif printf '%s' "$erreur" | grep -qi 'already\|must be unique'; then
      printf '  #%-3s déjà rattaché\n' "$enfant"
      deja=$((deja + 1))
    else
      printf '  #%-3s ÉCHEC : %s\n' "$enfant" "$(printf '%s' "$erreur" | head -1)"
      echecs=$((echecs + 1))
    fi
  done
done

printf '\n%s liens posés, %s déjà en place, %s échecs\n' "$poses" "$deja" "$echecs"
printf 'Vérifier : https://github.com/%s/issues/16\n' "$REPO"
[ "$echecs" -eq 0 ]
