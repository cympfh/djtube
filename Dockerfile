# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_CACHE_DIR=/tmp/.uv-cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

FROM base AS builder
COPY pyproject.toml uv.lock README.md ./
COPY djtube ./djtube
COPY main.py ./
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS runtime
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser djtube ./djtube
COPY --chown=appuser:appuser main.py ./
COPY docker/install-deno.py /tmp/install-deno.py
RUN mkdir -p /app/data && chown appuser:appuser /app/data \
    && python /tmp/install-deno.py \
    && rm /tmp/install-deno.py \
    && /opt/djtube/deno --version

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    DJTUBE_PLAYLISTS=/app/data/playlists.json \
    DJTUBE_COOKIES=/app/data/cookies.txt
USER appuser
EXPOSE 8098
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8098"]
