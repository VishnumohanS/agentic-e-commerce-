# Single image running either agent; the command selects which one.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY buyer_agent/ ./buyer_agent/
COPY merchant_agent/ ./merchant_agent/
COPY static/ ./static/
COPY data/catalog.json ./data/catalog.json
COPY scripts/ ./scripts/

# Run as a non-root user; the data directory must stay writable for SQLite.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:${PORT:-8000}/health || exit 1

# Overridden by docker-compose and the ECS task definition.
CMD ["uvicorn", "merchant_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
