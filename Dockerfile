FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --requirement requirements.lock
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY config/clients.example.yaml ./config/clients.example.yaml
RUN pip install --no-deps . && useradd --create-home --uid 10001 adbeam \
    && mkdir -p /app/data && chown -R adbeam:adbeam /app/data

FROM base AS test
COPY requirements-dev.lock ./
RUN pip install --requirement requirements-dev.lock
COPY tests ./tests
CMD ["sh", "-c", "ruff check app tests migrations && ruff format --check app tests migrations && pytest -q"]

FROM base AS runtime
USER adbeam
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "from pathlib import Path; from time import time; p=Path('/app/data/heartbeat'); assert p.exists() and time()-float(p.read_text())<120"
ENTRYPOINT ["python", "-m", "app.main"]
CMD ["bot"]
