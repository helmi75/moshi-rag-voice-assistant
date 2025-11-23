# Intégration avec Moshi

## Comment fonctionne le serveur Moshi

Le serveur Moshi utilise **Gradio** pour exposer une interface web et une API. Par défaut, il écoute sur le port **8998**, mais nous l'avons configuré pour utiliser le port **8091** dans notre setup.

## API Gradio

Gradio expose généralement une API sur `/api/predict` avec le format suivant :

```json
{
  "data": [input_data],
  "fn_index": 0
}
```

Cependant, l'API exacte peut varier selon la version de Moshi. Si l'API actuelle ne fonctionne pas, vous devrez peut-être :

1. **Vérifier l'API Gradio** : Accédez à `http://moshi:8091/api/docs` pour voir les endpoints disponibles
2. **Utiliser le client Python** : Alternative plus robuste (voir ci-dessous)

## Alternative : Utiliser le client Python directement

Si l'API HTTP ne fonctionne pas correctement, vous pouvez modifier `api/app/main.py` pour utiliser directement le client Python de Moshi :

```python
# Dans api/app/main.py
# Option 1: Importer le client Moshi (nécessite d'installer moshi dans le conteneur API)
from moshi.client import MoshiClient

# Option 2: Utiliser httpx pour appeler l'API Gradio avec le bon format
```

## Modèles disponibles

Vous pouvez changer le modèle en modifiant la variable `MOSHI_HF_REPO` dans votre `.env` :

- `kyutai/moshika-pytorch-bf16` (voix féminine, par défaut)
- `kyutai/moshiko-pytorch-bf16` (voix masculine)
- `kyutai/moshika-pytorch-q8` (quantifié 8 bits, expérimental)
- `kyutai/moshiko-pytorch-q8` (quantifié 8 bits, expérimental)

## Téléchargement des modèles

Les modèles sont **automatiquement téléchargés** depuis HuggingFace au premier démarrage et mis en cache dans le volume `moshi_cache`. Cela peut prendre plusieurs minutes selon votre connexion.

## Dépannage

### Le serveur Moshi ne démarre pas

1. Vérifiez les logs : `docker compose logs moshi`
2. Vérifiez que le GPU est disponible : `docker compose exec moshi nvidia-smi`
3. Vérifiez que le package moshi est installé : `docker compose exec moshi python3 -c "import moshi"`

### L'API ne répond pas

1. Vérifiez que le serveur Moshi est démarré : `docker compose ps`
2. Testez l'endpoint de santé : `curl http://localhost:8091`
3. Vérifiez les logs de l'API : `docker compose logs api`

### Erreurs de mémoire GPU

Si vous avez des erreurs de mémoire GPU, essayez :
- Utiliser le modèle quantifié q8 : `MOSHI_HF_REPO=kyutai/moshika-pytorch-q8`
- Réduire la taille du batch (déjà à 1 par défaut)
- Vérifier que vous avez bien 24 Go de VRAM

