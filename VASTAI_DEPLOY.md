# Guide de Déploiement sur Vast.ai

Ce guide explique comment déployer et faire fonctionner le projet Moshi sur Vast.ai.

## 📋 Prérequis

- Un compte Vast.ai avec des crédits
- Git installé localement
- Un compte Twilio avec un numéro de téléphone

## 🚀 Étape 1: Louer une Instance GPU

1. Allez sur [Vast.ai](https://vast.ai/console/create/)
2. Filtrez les instances avec ces critères:
   - **GPU**: RTX 3090, RTX 4090, A4000, A5000, A6000 (16GB+ VRAM)
   - **CUDA**: 12.0+
   - **Docker**: Activé
   - **Stockage**: 50GB minimum

3. Choisissez le template **Docker compose on startup** ou **PyTorch**

4. Lancez l'instance et notez l'IP publique

## 🔧 Étape 2: Configurer l'Instance

### Connexion SSH

```bash
# Connectez-vous avec les identifiants fournis par Vast.ai
ssh -p <PORT> root@<IP_PUBLIQUE>
```

### Cloner le projet

```bash
cd /workspace
git clone https://github.com/helmi75/porjet-oshi-vast.git
cd porjet-oshi-vast
```

### Configurer les variables d'environnement

```bash
cp env.example .env
nano .env
```

Modifiez le fichier `.env` avec vos vraies valeurs Twilio:

```env
TWILIO_ACCOUNT_SID="votre_account_sid"
TWILIO_AUTH_TOKEN="votre_auth_token"
TWILIO_NUMBER=+1234567890

# Pour GPU avec 16GB+ VRAM, utilisez le modèle complet:
MOSHI_HF_REPO=kyutai/moshika-pytorch-bf16

# Pour GPU avec 8-16GB VRAM, utilisez le modèle quantifié:
# MOSHI_HF_REPO=kyutai/moshika-pytorch-q8
```

## 🐳 Étape 3: Lancer les Services

```bash
# Construire et démarrer tous les services
docker compose up -d --build

# Vérifier que tout tourne
docker compose ps

# Suivre les logs (le téléchargement des modèles peut prendre 5-10 min)
docker compose logs -f moshi
```

### Vérification

Attendez que vous voyiez dans les logs:
```
Running on local URL: http://0.0.0.0:8091
```

Puis testez:

```bash
# Test de santé
curl http://localhost:8000/health

# Test e2e complet
chmod +x tests/test_e2e.sh
./tests/test_e2e.sh localhost
```

## 🌐 Étape 4: Configurer Twilio

### Option A: Configuration Automatique

```bash
pip install twilio python-dotenv
python setup_twilio.py
```

Entrez l'URL: `http://<IP_VAST>:80/twilio/webhook`

### Option B: Configuration Manuelle

1. Allez sur [Twilio Console](https://console.twilio.com/)
2. Naviguez vers **Phone Numbers** → Votre numéro
3. Dans la section **Voice & Fax**:
   - **A CALL COMES IN**: Webhook, `http://<IP_VAST>:80/twilio/voice`, POST
4. Dans la section **Messaging**:
   - **A MESSAGE COMES IN**: Webhook, `http://<IP_VAST>:80/twilio/sms`, POST
5. Sauvegardez

## 📞 Étape 5: Tester

### Test Vocal

1. Appelez votre numéro Twilio depuis un téléphone
2. Vous devriez entendre: "Bonjour, je suis votre assistant vocal..."
3. Parlez et attendez la réponse de Moshi

### Test SMS

1. Envoyez un SMS à votre numéro Twilio
2. Vous devriez recevoir une réponse générée par Moshi

## 🔍 Dépannage

### Moshi ne démarre pas

```bash
# Vérifier les logs
docker compose logs moshi

# Redémarrer
docker compose restart moshi
```

### Erreur CUDA Out of Memory

Passez au modèle quantifié dans `.env`:
```env
MOSHI_HF_REPO=kyutai/moshika-pytorch-q8
```

Puis:
```bash
docker compose up -d --build moshi
```

### Twilio ne peut pas atteindre le serveur

1. Vérifiez que les ports 80/443 sont ouverts sur Vast.ai
2. Testez l'accès depuis l'extérieur:
   ```bash
   curl http://<IP_VAST>:80/health
   ```

### Le modèle met du temps à charger

Le premier démarrage télécharge ~7GB de modèles. C'est normal que cela prenne 5-10 minutes.

## 💰 Optimisation des Coûts

- **Arrêtez l'instance** quand vous ne l'utilisez pas
- Utilisez **Spot instances** pour les tests (moins cher mais peut être interrompu)
- Le modèle est mis en cache dans un volume Docker, les redémarrages sont rapides

## 📊 Monitoring

```bash
# Voir l'utilisation GPU
nvidia-smi

# Voir les logs en temps réel
docker compose logs -f

# Voir l'état des conteneurs
docker compose ps
```
