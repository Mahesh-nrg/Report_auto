FROM python:3.12-slim

# Optional extras: --build-arg EXTRAS=rag,https  (local-document RAG, self-signed HTTPS)
ARG EXTRAS=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PWNDOC_AI_DATA_DIR=/app/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN if [ -n "$EXTRAS" ]; then pip install --no-cache-dir ".[${EXTRAS}]"; \
    else pip install --no-cache-dir .; fi \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app/data \
 && chown -R app:app /app/data

USER app
VOLUME ["/app/data"]
EXPOSE 8080
CMD ["pwndoc-ai", "serve", "--host", "0.0.0.0", "--port", "8080"]
