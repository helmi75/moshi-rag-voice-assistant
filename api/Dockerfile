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
# Sonde de VIE (pas d'état : ça, c'est /supervision). Sans elle, `docker compose ps`
# affiche « Up » pour un uvicorn bloqué qui ne répond plus. Python plutôt que curl :
# l'image slim n'a pas curl, et l'ajouter pour une sonde grossirait l'image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status != 200)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
