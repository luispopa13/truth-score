# TruthScore Backend -- Production Dockerfile
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# torch + torchvision CPU-only (varianta ta locala: 2.9.1+cpu)
# Instalate separat din indexul CPU al PyTorch -- mult mai mici decat varianta CUDA
RUN pip install --no-cache-dir \
    torch==2.9.1 torchvision==0.24.1 \
    --index-url https://download.pytorch.org/whl/cpu

# Restul dependintelor (torch e deja instalat, pip il sare)
RUN pip install --no-cache-dir -r requirements.txt

# Bake modelele locale in imagine la build time -- containerul deployat
# nu mai are nevoie sa descarce nimic de la HuggingFace la runtime
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
import os; \
os.makedirs('models', exist_ok=True); \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2').save('models/embed_en'); \
SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2').save('models/embed_multi'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2').save('models/crossencoder'); \
print('Models baked into image.')"

COPY . .

RUN mkdir -p data/case_study .truthscore_cache

# The actual listen port comes from $PORT (default 8000) — resolved at runtime
# by gunicorn.conf.py, not baked in here. EXPOSE is documentation only.
EXPOSE 8000

# Run the SAME process model as production (Procfile): gunicorn with the shared
# config. gunicorn.conf.py reads $HOST/$PORT (default 0.0.0.0:8000) for the bind
# and $WEB_CONCURRENCY/$GUNICORN_WORKERS for the worker count, so nothing here is
# hardcoded. Exec form is fine: the config reads the env vars at runtime.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "main:app"]