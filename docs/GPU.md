# Pocket TTS sur GPU (voix Kyutai temps réel)

Sur CPU, la voix française de Pocket TTS (`french_24l`) met **5 à 10 s** à produire
son premier morceau — au-delà du seuil de Pipecat, d'où l'audio **saccadé** ou perdu.
Sur GPU, la génération passe **sous la seconde** : la voix Kyutai devient utilisable en
temps réel au téléphone.

## Cartes compatibles

Cette image cible **Maxwell et plus récent** : GTX 900 (ex. **GTX 980 Ti**, compute
5.2), GTX 10xx (Pascal), RTX, etc.

> **Pourquoi une version de PyTorch figée ?** PyTorch > 2.5 a **retiré** les noyaux
> CUDA de l'architecture Maxwell, et CUDA 13.x aussi. `Dockerfile.gpu` épingle donc
> **PyTorch 2.5.1 + CUDA 12.1**, la dernière lignée qui garde Maxwell *et* satisfait
> pocket-tts (`torch >= 2.5`). Sur une carte plus récente, cette image fonctionne
> aussi (rétro-compatible).

La 980 Ti a **6 Go de VRAM**, largement assez pour Pocket TTS (le modèle est petit).

## Prérequis côté hôte

1. **Pilote NVIDIA récent** (≥ 525, compatible CUDA 12.1). Vérifier : `nvidia-smi`.
2. **nvidia-container-toolkit** installé et configuré pour Docker, pour exposer le
   GPU au conteneur :
   ```bash
   # Debian/Ubuntu (voir la doc NVIDIA pour votre distribution)
   sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
   Test rapide que Docker voit le GPU :
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
   ```

## Lancement

Dans votre `.env` :
```
VOICE_MODE=stream
TTS_PROVIDER=pocket          # défaut
POCKET_TTS_DEVICE=cuda       # forcé par la surcouche GPU de toute façon
DEEPGRAM_API_KEY=...         # STT streaming
PUBLIC_WS_URL=wss://VOTRE-TUNNEL.ngrok-free.app/ws/voice
```

Puis, en ajoutant la surcouche GPU :
```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```
La première construction est longue (téléchargement de PyTorch CUDA, ~2,5 Go). Au
premier appel, ~30-60 s de warm-up (téléchargement du modèle Pocket TTS, mis en cache
dans le volume), puis la voix Kyutai en temps réel.

## Vérifier que le GPU est bien utilisé

Dans les logs :
```
docker compose logs api | grep -i "device\|GPU"
```
Vous devez voir `Pocket TTS : modèle déplacé sur cuda (GPU).` et
`Pocket TTS prêt (24000 Hz, device=cuda) ...`. Pendant un appel, la ligne
`Pocket TTS : ... (xN.NN temps réel)` doit afficher un facteur **> 1** (plus rapide
que le temps réel) et un 1er chunk bien **sous la seconde**.

En parallèle, `nvidia-smi` sur l'hôte doit montrer le process Python et de la VRAM
occupée pendant la génération.

## Repli

- `POCKET_TTS_DEVICE=cuda` (surcouche) échoue franchement si le GPU n'est pas visible
  (erreur dans les logs) — utile pour diagnostiquer. Mettez `auto` pour un repli CPU
  silencieux.
- Sans GPU exploitable, restez sur `VOICE_MODE=gather` (voix neuronale Polly, fiable)
  ou `TTS_PROVIDER=cartesia` (API temps réel).

## Et la « vraie » voix d'unmute.sh (Kyutai TTS 1.6B) ?

Le modèle exact d'unmute.sh est le **1.6B**, servi par `moshi-server` (Rust) et pesant
~5,3 Go de VRAM. Sur 6 Go c'est jouable (STT et LLM sont dans le cloud, seul le TTS
occupe le GPU) mais **serré**, et la compatibilité Maxwell y est moins certaine. À
tenter en phase B une fois Pocket-sur-GPU validé.
