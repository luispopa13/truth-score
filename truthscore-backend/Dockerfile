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

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]