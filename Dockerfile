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
RUN mkdir -p /app/data && chown appuser:appuser /app/data \
    && python - <<'PY'
import hashlib, io, platform, urllib.request, zipfile
from pathlib import Path

version = "2.9.7"
sha256 = {
    "x86_64": "c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490",
    "aarch64": "c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf",
}
arch = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
machine = platform.machine()
digest = sha256[machine]
url = f"https://github.com/denoland/deno/releases/download/v{version}/deno-{arch[machine]}.zip"
data = urllib.request.urlopen(url, timeout=180).read()
if hashlib.sha256(data).hexdigest() != digest:
    raise SystemExit("deno checksum mismatch")
binary = Path("/opt/djtube/deno")
binary.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(data)) as archive:
    member = next(name for name in archive.namelist() if Path(name).name == "deno")
    binary.write_bytes(archive.read(member))
binary.chmod(0o755)
PY

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    DJTUBE_PLAYLISTS=/app/data/playlists.json \
    DJTUBE_COOKIES=/app/data/cookies.txt
USER appuser
EXPOSE 8098
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8098"]
