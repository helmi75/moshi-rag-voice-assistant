#!/bin/bash
set -euo pipefail
cd /opt/moshi

: "${MOSHI_MODEL_DIR:=/models/moshi}"
: "${MOSHI_DEVICE:=gpu}"
: "${MOSHI_BATCH_SIZE:=1}"
: "${MOSHI_PORT:=8091}"
: "${MOSHI_HOST:=0.0.0.0}"

# Ensure models dir exists (mounted from host)
mkdir -p "${MOSHI_MODEL_DIR}"

# Vérifier que le répertoire des modèles contient des fichiers
if [ ! "$(ls -A ${MOSHI_MODEL_DIR} 2>/dev/null)" ]; then
    echo "WARNING: Le répertoire ${MOSHI_MODEL_DIR} est vide."
    echo "Assurez-vous que les fichiers du modèle Moshi sont montés dans ce répertoire."
fi

echo "=== Démarrage du serveur Moshi ==="
echo "MODEL_DIR: ${MOSHI_MODEL_DIR}"
echo "DEVICE: ${MOSHI_DEVICE}"
echo "PORT: ${MOSHI_PORT}"
echo "HOST: ${MOSHI_HOST}"

# Vérifier la disponibilité du GPU si device=gpu
if [ "${MOSHI_DEVICE}" = "gpu" ]; then
    if command -v nvidia-smi &> /dev/null; then
        echo "GPU détecté:"
        nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    else
        echo "WARNING: nvidia-smi non disponible. Vérifiez la configuration GPU."
    fi
fi

# Stratégie de démarrage: essayer plusieurs méthodes dans l'ordre
# 1. Script de démarrage du repo si disponible
if [ -f ./scripts/run_server.sh ]; then
    echo "Méthode 1: Utilisation de scripts/run_server.sh"
    exec bash ./scripts/run_server.sh \
        --model-dir "${MOSHI_MODEL_DIR}" \
        --device "${MOSHI_DEVICE}" \
        --port "${MOSHI_PORT}" \
        --host "${MOSHI_HOST}" || true
fi

# 2. Module Python moshi.server
if python3 -c "import moshi" 2>/dev/null; then
    echo "Méthode 2: Utilisation du module Python moshi.server"
    exec python3 -m moshi.server \
        --model-dir "${MOSHI_MODEL_DIR}" \
        --device "${MOSHI_DEVICE}" \
        --port "${MOSHI_PORT}" \
        --host "${MOSHI_HOST}" || true
fi

# 3. Commande directe Python si le module existe
if [ -f setup.py ] || [ -f pyproject.toml ]; then
    echo "Méthode 3: Tentative avec la commande Python directe"
    exec python3 -m moshi \
        --model-dir "${MOSHI_MODEL_DIR}" \
        --device "${MOSHI_DEVICE}" \
        --port "${MOSHI_PORT}" \
        --host "${MOSHI_HOST}" || true
fi

# 4. Fallback: serveur HTTP simple (pour debug uniquement)
echo "ERREUR: Aucune méthode de démarrage valide trouvée."
echo "Veuillez vérifier le README du repo Moshi et mettre à jour entrypoint.sh"
echo "Démarrage d'un serveur HTTP de fallback sur le port ${MOSHI_PORT}..."
exec python3 -m http.server "${MOSHI_PORT}"
