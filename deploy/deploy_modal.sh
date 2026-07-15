#!/usr/bin/env bash
# Déploiement une-commande de l'assistant vocal (voix Kyutai 1.6B) sur Modal.
#
#   ./deploy/deploy_modal.sh
#
# Prérequis (une seule fois) :
#   pip install modal && modal setup
#   un fichier .env à la racine (copié de env.example, rempli)
#   licence acceptée sur https://huggingface.co/kyutai/tts-1.6b-en_fr (+ HF_TOKEN dans .env si requis)
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v modal >/dev/null 2>&1; then
  echo "❌ 'modal' introuvable. Installez-le : pip install modal && modal setup"
  exit 1
fi

if [ ! -f .env ]; then
  echo "❌ Fichier .env manquant à la racine. Créez-le : cp env.example .env  (puis remplissez)"
  exit 1
fi

echo "🚀 Déploiement sur Modal (image GPU + envoi du .env + préchauffage du 1.6B)..."
echo "   (la première fois, la construction de l'image peut prendre plusieurs minutes)"
modal deploy deploy/modal_app.py

cat <<'EOF'

✅ Déployé.

Étapes finales :
  1. Copiez l'URL publique affichée ci-dessus (…modal.run).
  2. Pointez votre numéro Twilio dessus (webhook voix) :
       python3 scripts/twilio_setup_number.py --webhook https://VOTRE-URL.modal.run/twilio/webhook
  3. Appelez. Les logs en direct :  modal app logs moshi-voice-assistant

Astuce coût : min_containers=1 garde une box chaude (pas de cold start). Pour couper
la nuit, redéployez avec  MODAL_MIN_CONTAINERS=0 ./deploy/deploy_modal.sh
(le 1er appel après inactivité subira alors le chargement du modèle).
EOF
