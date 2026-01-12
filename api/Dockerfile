FROM python:3.12-slim

WORKDIR /app

# Copier d'abord requirements.txt pour utiliser le cache Docker
COPY ./app/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copier le reste de l'application
COPY ./app /app/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
