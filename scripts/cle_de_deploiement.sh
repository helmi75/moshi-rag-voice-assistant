#!/usr/bin/env bash
# Pose la clé SSH de déploiement sur le runner GitHub (ASSISTANTE-176), depuis les secrets
# de l'environnement : CLE (la clé privée dédiée) et HOTES (la ligne known_hosts du
# serveur). Sans HOTES, ssh accepterait n'importe quel serveur se présentant à cette
# adresse : refus. Rien n'est affiché.
set -euo pipefail

[ -n "${CLE:-}" ] || { echo "✗ secret DEPLOY_SSH_KEY absent de l'environnement : déploiement depuis GitHub pas encore branché (docs/DEPLOY.md, « Depuis GitHub »)." >&2; exit 1; }
[ -n "${HOTES:-}" ] || { echo "✗ secret DEPLOY_KNOWN_HOSTS absent : on ne parle pas à un serveur qu'on ne sait pas reconnaître." >&2; exit 1; }

install -d -m 700 ~/.ssh
( umask 077; printf '%s\n' "$CLE" > ~/.ssh/moshi-vps-deploy )
printf '%s\n' "$HOTES" > ~/.ssh/known_hosts
# Une clé collée sans son dernier saut de ligne, ou avec des retours chariot, est refusée
# par ssh avec un message obscur : on le dit ici.
ssh-keygen -y -f ~/.ssh/moshi-vps-deploy >/dev/null 2>&1 \
  || { echo "✗ DEPLOY_SSH_KEY n'est pas une clé privée lisible (sans phrase de passe)." >&2; exit 1; }
echo "clé de déploiement posée."
