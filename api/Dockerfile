FROM python:3.12-slim

WORKDIR /app

# libgomp1 : requis par onnxruntime (VAD Silero / smart-turn du pipeline vocal).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Le verrou d'abord, seul : la couche pip n'est reconstruite que si une version change,
# pas à chaque ligne de code. `requirements.lock` est la liste EXACTE des versions
# installées en production ; `requirements.txt` n'est que l'intention. Sans verrou,
# chaque `docker compose up --build` réinstallait « la dernière » version de FastAPI,
# Starlette, OpenAI, websockets… : la production pouvait casser sans un commit.
# Régénération : docs/DEPLOY.md, section « Dépendances figées ».
COPY ./app/requirements.lock /app/requirements.lock
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r /app/requirements.lock

COPY ./app /app/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
