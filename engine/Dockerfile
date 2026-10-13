# Build context is the repository root.
FROM python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/models
WORKDIR /app/engine
COPY engine/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Bake the embedding model into the image so no download happens at runtime.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" \
    && chmod -R a+rX /opt/models
# The model is baked above; the hub client stays offline at runtime.
ENV HF_HUB_OFFLINE=1
COPY simple-creds-storage /opt/simple-creds-storage
RUN pip install --no-cache-dir /opt/simple-creds-storage
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin engine
# A fresh keys volume copies this owner and mode, so uid 10001 can create the store.
RUN mkdir -p /data/keys && chown 10001:10001 /data/keys && chmod 700 /data/keys
COPY engine/app ./app
COPY engine/lore ./lore

FROM base AS runtime
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]

FROM base AS test
RUN apt-get update \
    && apt-get install -y --no-install-recommends git
COPY engine/requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY engine/pyproject.toml ./
COPY engine/tests ./tests
COPY examples/ /app/examples/
USER 10001
CMD ["pytest", "-p", "no:cacheprovider", "tests"]
