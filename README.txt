Projet Moshi Voice Assistant (Vast.ai)
------------------------------------
Contenu: docker-compose with moshi (cloned from kyutai-labs/moshi), a FastAPI backend (reservation + RAG stub), and Caddy for TLS reverse proxy.

IMPORTANT:
- Model weights are NOT included. Mount your model files into ./volumes/models on the host.
- The moshi entrypoint.sh contains a placeholder startup command. After pulling the repo upstream, inspect its README and replace the startup command in moshi/entrypoint.sh with the recommended production command (pytorch or rust backend).
- This package automates image build and startup for convenience on Vast.ai.

Quickstart on the remote instance (Vast.ai):
1. Upload this zip on the instance, unzip:
   unzip projet-moshi-vast.zip -d projet-moshi
2. Fill .env from .env.template
3. Ensure Docker + docker-compose are installed, and nvidia-container-runtime configured.
4. Place Moshi model files into ./volumes/models or ensure network access for download.
5. Build and start:
   docker compose up -d --build
6. Watch logs:
   docker compose logs -f moshi
   docker compose logs -f api

If you want, provide credentials, domain names, and whether you want the rust or pytorch backend; I will update entrypoint and dockerfile to match exactly.
