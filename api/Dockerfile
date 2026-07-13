FROM python:3.12-slim

WORKDIR /app

# libgomp1  : requis par onnxruntime (VAD Silero / smart-turn du pipeline vocal)
# libsndfile1 : requis par soundfile (chargement des voix Pocket TTS)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Copier d'abord requirements.txt pour utiliser le cache Docker
COPY ./app/requirements.txt /app/requirements.txt
# setuptools/wheel récents : certaines dépendances (docopt) ne buildent pas sans
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt

# Copier le reste de l'application
COPY ./app /app/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
