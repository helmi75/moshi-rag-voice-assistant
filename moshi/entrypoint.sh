#!/bin/bash
set -euo pipefail

: "${MOSHI_HF_REPO:=kyutai/moshika-pytorch-bf16}"
: "${MOSHI_PORT:=8091}"
: "${MOSHI_HOST:=0.0.0.0}"

echo "=== Démarrage du serveur Moshi ==="
echo "HF_REPO: ${MOSHI_HF_REPO}"
echo "PORT: ${MOSHI_PORT}"
echo "HOST: ${MOSHI_HOST}"

# Vérifier la disponibilité du GPU
if command -v nvidia-smi &> /dev/null; then
    echo "GPU détecté:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
else
    echo "WARNING: nvidia-smi non disponible. Vérifiez la configuration GPU."
fi

# Vérifier que le module moshi est installé
if ! python3 -c "import moshi" 2>/dev/null; then
    echo "ERREUR: Le module 'moshi' n'est pas installé."
    echo "Installez-le avec: pip install -U moshi"
    exit 1
fi

# Démarrer le serveur Moshi avec les paramètres corrects
# Le serveur télécharge automatiquement les modèles depuis HuggingFace si nécessaire
# Les modèles seront mis en cache dans ~/.cache/huggingface/
echo "Démarrage du serveur Moshi..."
echo "Les modèles seront téléchargés automatiquement depuis HuggingFace si nécessaire."
echo "Premier démarrage peut prendre du temps pour télécharger les modèles."

# Commande selon le README officiel de Moshi
# python -m moshi.server [--gradio-tunnel] [--hf-repo kyutai/moshika-pytorch-bf16]
exec python3 -m moshi.server \
    --hf-repo "${MOSHI_HF_REPO}" \
    --port "${MOSHI_PORT}" \
    --host "${MOSHI_HOST}"
