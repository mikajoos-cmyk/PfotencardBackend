# 1. Offizielles, leichtes Python-Image als Basis
FROM python:3.11-slim

# 2. Arbeitsverzeichnis im Container setzen
WORKDIR /app

# 3. System-Bibliotheken für WeasyPrint (PDF-Generierung) installieren
RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    python3-cffi \
    libcairo2 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libjpeg-dev \
    libopenjp2-7-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 4. Abhängigkeiten kopieren und installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Den restlichen Code deines Backends kopieren
COPY . .

# 6. Den FastAPI Server starten (WICHTIG: Host 0.0.0.0 für Render)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]