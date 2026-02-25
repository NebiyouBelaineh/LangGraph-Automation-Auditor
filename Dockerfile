# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS base

# git is required at runtime for sandboxed repo cloning
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ── Dependency layer (cached unless pyproject.toml / uv.lock change) ─────────
FROM base AS deps

COPY pyproject.toml uv.lock ./

# Install production deps into an isolated virtualenv inside the image
RUN uv sync --frozen --no-dev --no-install-project

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM base AS runtime

# Copy the pre-built virtualenv from the deps stage
COPY --from=deps /app/.venv /app/.venv

# Activate the virtualenv for all subsequent commands
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Source last — code changes don't invalidate the dependency cache
COPY src/ ./src/
COPY main.py ./

# Writable directories, mountable as volumes at runtime
RUN mkdir -p output reports

# Non-root user for defence-in-depth
RUN useradd --no-create-home --shell /bin/false auditor \
    && chown -R auditor:auditor /app
USER auditor

ENTRYPOINT ["python", "main.py"]
