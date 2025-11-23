# Projet Moshi Voice Assistant (Vast.ai)

Assistant vocal intelligent basé sur Moshi (Kyutai Labs) déployé sur Vast.ai avec support GPU.

## 🚀 Fonctionnalités

- **Moshi AI** : Modèle de voix conversationnelle avancé
- **API FastAPI** : Backend pour gestion des réservations et RAG
- **Caddy** : Reverse proxy avec support TLS
- **Support GPU** : Optimisé pour GPU NVIDIA (24 Go recommandé)

## 📋 Prérequis

- Instance Vast.ai avec GPU NVIDIA (24 Go recommandé)
- Docker et Docker Compose installés
- `nvidia-container-toolkit` configuré
- Fichiers de modèle Moshi

## 🛠️ Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/helmi75/porjet-oshi-vast.git
cd porjet-oshi-vast
```

### 2. Configuration des variables d'environnement

```bash
cp env.example .env
# Éditez .env avec vos vraies valeurs Twilio
```

### 3. Configuration du modèle (optionnel)

Les modèles Moshi sont **automatiquement téléchargés** depuis HuggingFace au premier démarrage. Par défaut, le modèle `kyutai/moshika-pytorch-bf16` (voix féminine) est utilisé.

Pour changer de modèle, modifiez `MOSHI_HF_REPO` dans votre `.env` :
- `kyutai/moshika-pytorch-bf16` (voix féminine, par défaut)
- `kyutai/moshiko-pytorch-bf16` (voix masculine)
- `kyutai/moshika-pytorch-q8` (quantifié 8 bits, expérimental)

### 4. Démarrer les services

```bash
docker compose up -d --build
```

### 5. Surveiller les logs

```bash
# Logs Moshi
docker compose logs -f moshi

# Logs API
docker compose logs -f api

# Tous les logs
docker compose logs -f
```

## 📁 Structure du projet

```
projet-moshi-vast/
├── api/                 # API FastAPI
│   ├── app/
│   │   ├── main.py      # Point d'entrée API
│   │   ├── rag.py       # RAG (Recherche augmentée)
│   │   └── reservations.py
│   └── Dockerfile
├── moshi/               # Service Moshi
│   ├── Dockerfile
│   └── entrypoint.sh
├── caddy/               # Reverse proxy
│   └── Caddyfile
├── volumes/
│   ├── models/          # Modèles ML (non versionné)
│   └── data/            # Données (non versionné)
├── docker-compose.yml
├── env.example
└── README.md
```

## 🔧 Configuration

### Variables d'environnement

- `TWILIO_ACCOUNT_SID` : Identifiant compte Twilio
- `TWILIO_AUTH_TOKEN` : Token d'authentification Twilio
- `TWILIO_NUMBER` : Numéro de téléphone Twilio
- `MOSHI_HF_REPO` : Repository HuggingFace du modèle (défaut: `kyutai/moshika-pytorch-bf16`)
- `MOSHI_PORT` : Port du serveur Moshi (défaut: `8091`)
- `MOSHI_HOST` : Host du serveur Moshi (défaut: `0.0.0.0`)

### Ports

- **80** : Caddy (HTTP)
- **443** : Caddy (HTTPS)
- **8000** : API FastAPI
- **8091** : Service Moshi

## 🐛 Dépannage

### Vérifier la disponibilité du GPU

```bash
docker compose exec moshi nvidia-smi
```

### Redémarrer un service

```bash
docker compose restart moshi
docker compose restart api
```

### Reconstruire les images

```bash
docker compose up -d --build --force-recreate
```

## 📝 Notes importantes

- Les modèles sont **automatiquement téléchargés** depuis HuggingFace au premier démarrage
- Les modèles sont mis en cache dans le volume Docker `moshi_cache` pour éviter les re-téléchargements
- Le premier démarrage peut prendre plusieurs minutes pour télécharger les modèles (plusieurs GB)
- Ce package automatise le build et le démarrage pour faciliter le déploiement sur Vast.ai
- Le serveur Moshi utilise Gradio qui expose une interface web sur le port configuré

## 🔗 Liens utiles

- [Moshi (Kyutai Labs)](https://github.com/kyutai-labs/moshi)
- [Moshi Demo](https://moshi.chat)
- [Modèles HuggingFace](https://huggingface.co/collections/kyutai/moshi-v01-release-66eaeaf3302bef6bd9ad7acd)
- [Vast.ai](https://vast.ai)
- [Docker Compose](https://docs.docker.com/compose/)

## 📚 Documentation supplémentaire

- [MOSHI_INTEGRATION.md](MOSHI_INTEGRATION.md) : Guide détaillé sur l'intégration avec Moshi
- [TWILIO_SETUP.md](TWILIO_SETUP.md) : **Guide complet pour configurer Twilio**
- [GITHUB_SETUP.md](GITHUB_SETUP.md) : Instructions pour créer le dépôt GitHub

## 📞 Configuration Twilio

Pour connecter votre numéro Twilio à l'application :

1. **Configuration manuelle** : Suivez le guide [TWILIO_SETUP.md](TWILIO_SETUP.md)
2. **Configuration automatique** : Utilisez le script `setup_twilio.py` :
   ```bash
   pip install twilio python-dotenv
   python setup_twilio.py
   ```

L'URL du webhook à configurer dans Twilio est : `https://VOTRE_DOMAINE/twilio/webhook`

## 📄 Licence

Voir le fichier LICENSE pour plus de détails.

## 👤 Auteur

**helmi75**

---

⭐ N'oubliez pas de mettre une étoile si ce projet vous est utile !

