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

### 3. Préparer les modèles

Placez les fichiers du modèle Moshi dans `./volumes/models/moshi/`

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
- `MOSHI_MODEL_DIR` : Chemin vers les modèles (défaut: `/models/moshi`)
- `MOSHI_DEVICE` : Device à utiliser (`gpu` ou `cpu`)
- `MOSHI_BATCH_SIZE` : Taille du batch (défaut: `1`)

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

- Les poids des modèles ne sont **PAS** inclus dans ce dépôt
- Montez vos fichiers de modèle dans `./volumes/models` sur l'hôte
- Le script `moshi/entrypoint.sh` doit être ajusté selon le README du repo Moshi officiel
- Ce package automatise le build et le démarrage pour faciliter le déploiement sur Vast.ai

## 🔗 Liens utiles

- [Moshi (Kyutai Labs)](https://github.com/kyutai-labs/moshi)
- [Vast.ai](https://vast.ai)
- [Docker Compose](https://docs.docker.com/compose/)

## 📄 Licence

Voir le fichier LICENSE pour plus de détails.

## 👤 Auteur

**helmi75**

---

⭐ N'oubliez pas de mettre une étoile si ce projet vous est utile !

