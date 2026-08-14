# syntax=docker/dockerfile:1
# API image. Builds the UI in a node stage and serves it from the Python app,
# so the deployed stack is one origin and one fewer container on a 1 GB VM.

# --- stage 1: build the UI --------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- stage 2: python runtime ------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/models \
    FLASHRANK_CACHE_DIR=/opt/models/flashrank

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# CPU-only torch first: the default wheel pulls ~2.5 GB of CUDA that a VM with
# no GPU will never use.
COPY requirements.txt ./
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# Bake the embedding and rerank models into the image. Downloading them on first
# request would make a cold container's first query time out.
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', device='cpu'); \
from flashrank import Ranker; \
Ranker(model_name='ms-marco-MiniLM-L-12-v2', cache_dir='/opt/models/flashrank'); \
print('models cached')"

COPY app ./app
COPY scripts ./scripts
COPY data ./data
COPY --from=ui /ui/dist ./web/dist

# Non-root: nothing here needs to write outside /srv and /opt/models.
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /srv /opt/models
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
