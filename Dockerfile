# Applyuminati -- single deployable image.
#
# One container serves both halves of the product from the same origin: the
# built React bundle at "/" and the JSON API at "/api/v1". That is a deliberate
# deployment decision, not laziness -- it removes CORS configuration, removes a
# build-time API base URL, and makes the compose stack a single service that can
# be pasted into Portainer and reached on one published port.
#
# Three stages:
#   web    -- node, builds the SPA into /app/apps/web/dist
#   python -- uv, resolves dependencies and installs the project into /opt/venv
#   runtime-- python:3.12-slim, carries only the venv, the assets and the source
#
# Layer order is dependency-manifests-before-source in both build stages, so
# editing application code never re-resolves npm or Python dependencies.

# ---------------------------------------------------------------------------
# Stage 1: web assets
# ---------------------------------------------------------------------------
FROM node:22-alpine AS web

WORKDIR /app/apps/web

# Manifests first. The glob also picks up package-lock.json when it exists,
# without failing the build when it does not.
COPY apps/web/package*.json ./

# `npm ci` is the reproducible path and requires a lockfile; fall back to
# `npm install` for a fresh checkout that has not committed one yet.
RUN npm ci || npm install

COPY apps/web/ ./

RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Python dependencies and project install
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS python-build

# uv ships as a static binary; copying it from the published image avoids a
# curl|sh bootstrap and pins the tool alongside the rest of the build.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: manifests only. `uv.lock*` is a glob so the build works
# both before and after the lockfile is committed.
COPY pyproject.toml uv.lock* ./

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-dev --no-install-project; \
    else \
        uv sync --no-dev --no-install-project; \
    fi

# Project layer: source plus the files the build backend reads (readme and
# license are referenced by pyproject and their absence fails the wheel build).
COPY README.md LICENSE ./
COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-dev; \
    else \
        uv sync --no-dev; \
    fi

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Applyuminati" \
      org.opencontainers.image.description="Local-first, autonomous, LLM-powered job search and application platform." \
      org.opencontainers.image.source="https://github.com/spencerthayer/Applyuminati" \
      org.opencontainers.image.licenses="Apache-2.0"

# Unprivileged runtime identity. A fixed high uid/gid keeps file ownership on a
# bind-mounted /data predictable across hosts.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APPLYUMINATI_DATA_DIR=/data \
    APPLYUMINATI_ENVIRONMENT=docker \
    APPLYUMINATI_SERVER__HOST=0.0.0.0 \
    APPLYUMINATI_SERVER__PORT=8000 \
    APPLYUMINATI_SERVER__WEB_DIST=/app/web

WORKDIR /app

COPY --from=python-build /opt/venv /opt/venv

# The built SPA. APPLYUMINATI_SERVER__WEB_DIST above points the API at it.
COPY --from=web /app/apps/web/dist /app/web

# alembic.ini resolves script_location relative to the working directory, so the
# migration tree has to be present on disk even though the package itself is
# importable from the venv.
COPY alembic.ini ./
COPY src/ ./src/
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Created before VOLUME so the declared volume inherits app ownership on first
# mount; without this a fresh named volume would be root-owned and unwritable.
RUN mkdir -p /data && chown -R app:app /data

VOLUME ["/data"]
EXPOSE 8000

# Uses the interpreter already in the image rather than adding curl or wget.
# urlopen raises on any non-2xx status, which is exactly the failure signal.
# The port is read from the environment so overriding APPLYUMINATI_SERVER__PORT
# cannot silently leave the healthcheck probing the wrong socket forever.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('APPLYUMINATI_SERVER__PORT', '8000') + '/api/v1/health', timeout=4)"

USER app

ENTRYPOINT ["/app/entrypoint.sh"]
