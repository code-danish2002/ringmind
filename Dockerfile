FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 bot
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the knowledge-base embedding model (~80 MB) into the image so the first call isn't slow.
USER bot
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warm up'])"
USER root

COPY app ./app
COPY config ./config
COPY db ./db
COPY scripts ./scripts
COPY data/kb ./data/kb
RUN mkdir -p /data && chown bot:bot /data

USER bot
EXPOSE 8000 9092
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "20"]
