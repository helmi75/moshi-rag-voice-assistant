#!/usr/bin/env bash
#
# Sauvegarde quotidienne de la base de production (VPS).
#
# Pourquoi `.backup` et pas `cp` : la base est vivante. Copier le fichier pendant
# qu'une réservation s'écrit produit un fichier corrompu — et on ne s'en aperçoit
# que le jour où on essaie de le restaurer. `.backup` utilise l'API de sauvegarde
# en ligne de SQLite, qui gère le verrouillage et rend une copie cohérente.
#
# L'archive n'est nommée définitivement qu'APRÈS vérification d'intégrité : un
# fichier `app-*.db.gz` présent est donc, par construction, un fichier restaurable.
# Une sauvegarde partielle reste cachée (préfixe `.`) et n'est jamais prise pour
# une bonne.
#
# Installation (sur le VPS, en root) :
#   apt-get install -y sqlite3
#   install -m 755 scripts/backup-db.sh /opt/backups/backup-db.sh
#   ( crontab -l 2>/dev/null; echo "0 4 * * * /opt/backups/backup-db.sh >> /var/log/helmane-backup.log 2>&1" ) | crontab -
#
# Restauration : voir docs/DEPLOY.md, section Sauvegardes.
#
# Copie HORS du serveur : si RCLONE_REMOTE est posé (dans /opt/backups/backup.env, lu
# ci-dessous), l'archive vérifiée part aussi vers ce remote rclone (stockage objet EU).
# Sans elle, un incident disque emporte l'original ET les sauvegardes : les copies
# locales protègent d'une erreur de manipulation, pas de la perte de la machine.
#
# ⚠️ CE SCRIPT NE SAUVEGARDE QUE `app.db`, et c'est délibéré pour les enregistrements
# d'appels (#88). Les dupliquer dans des archives à rétention 14 jours étendrait
# l'exposition sur la donnée la plus sensible du produit — la voix — sans rien apporter :
# perdre un enregistrement de diagnostic est sans conséquence. Effet de bord heureux,
# `rgpd.effacer_appelant()` est COMPLET sur l'audio, ce qu'il n'est pas sur le transcript.

set -euo pipefail

# Réglages propres à la machine (RCLONE_REMOTE…), gardés hors du dépôt et hors du cron.
CONFIG="${CONFIG:-/opt/backups/backup.env}"
if [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG"
fi

DB="${DB:-/var/lib/docker/volumes/moshi-rag-voice-assistant_api_data/_data/app.db}"
DEST="${DEST:-/opt/backups/db}"
RETENTION_JOURS="${RETENTION_JOURS:-14}"
# Jeton lu par la supervision (api/app/supervision.py). Il vit dans le VOLUME de
# données, seul endroit que l'application voie : le cron tourne sur l'hôte, et
# `/opt/backups` n'existe pas pour le conteneur. Écrit APRÈS le contrôle d'intégrité,
# donc un jeton frais atteste d'une sauvegarde restaurable — pas d'un cron qui a
# simplement démarré.
JETON="${JETON:-$(dirname "$DB")/derniere-sauvegarde}"
# Second jeton, écrit seulement quand la copie distante est confirmée. La supervision
# le cherche à côté du premier (même nom + « -distante »).
JETON_DISTANT="${JETON_DISTANT:-$JETON-distante}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
RETENTION_DISTANTE_JOURS="${RETENTION_DISTANTE_JOURS:-30}"

horodate() { date -Is; }

if [ ! -f "$DB" ]; then
  echo "$(horodate) ÉCHEC : base introuvable ($DB)" >&2
  exit 1
fi

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M)"
TMP="$DEST/.app-$STAMP.db"

# Nettoyage si le script est interrompu : pas de fichier partiel qui traîne.
trap 'rm -f "$TMP" "$TMP.gz"' EXIT

sqlite3 "$DB" ".backup '$TMP'"

if [ "$(sqlite3 "$TMP" 'PRAGMA integrity_check;')" != "ok" ]; then
  echo "$(horodate) ÉCHEC : la copie ne passe pas le contrôle d'intégrité" >&2
  exit 1
fi

RESAS="$(sqlite3 "$TMP" 'SELECT count(*) FROM reservations;' 2>/dev/null || echo '?')"
TENANTS="$(sqlite3 "$TMP" 'SELECT count(*) FROM tenants;' 2>/dev/null || echo '?')"

gzip -9 "$TMP"
mv "$TMP.gz" "$DEST/app-$STAMP.db.gz"
trap - EXIT

find "$DEST" -name 'app-*.db.gz' -mtime "+$RETENTION_JOURS" -delete

TAILLE="$(du -h "$DEST/app-$STAMP.db.gz" | cut -f1)"

# Le jeton porte la date en UTC : la supervision compare des heures, pas des fuseaux.
date -u +%Y-%m-%dT%H:%M:%SZ > "$JETON" || \
  echo "$(horodate) AVERTISSEMENT : jeton de supervision non écrit ($JETON)" >&2

echo "$(horodate) ok — $TAILLE, $RESAS réservations, $TENANTS établissements"

if [ -z "$RCLONE_REMOTE" ]; then
  echo "$(horodate) AVERTISSEMENT : aucune copie hors du serveur (RCLONE_REMOTE vide)" >&2
  exit 0
fi

ARCHIVE="app-$STAMP.db.gz"
# `rclone copy` compare taille et somme de contrôle après l'envoi ; la relecture de la
# liste distante prouve en plus que l'archive est visible là où on ira la chercher.
if rclone copy "$DEST/$ARCHIVE" "$RCLONE_REMOTE" \
   && rclone lsf "$RCLONE_REMOTE" --include "$ARCHIVE" | grep -qx "$ARCHIVE"; then
  date -u +%Y-%m-%dT%H:%M:%SZ > "$JETON_DISTANT" || \
    echo "$(horodate) AVERTISSEMENT : jeton distant non écrit ($JETON_DISTANT)" >&2
  echo "$(horodate) copie distante ok — $RCLONE_REMOTE/$ARCHIVE"
else
  # La sauvegarde locale est bonne ; seule la copie distante manque. Code d'erreur
  # quand même, pour que le journal du cron le montre, et le jeton distant vieillit :
  # la supervision passe en attention sans qu'on ait à lire ce journal.
  echo "$(horodate) ÉCHEC : copie distante impossible vers $RCLONE_REMOTE" >&2
  exit 1
fi

rclone delete "$RCLONE_REMOTE" --min-age "${RETENTION_DISTANTE_JOURS}d" \
  --include 'app-*.db.gz' || \
  echo "$(horodate) AVERTISSEMENT : purge distante impossible (rétention non appliquée)" >&2
