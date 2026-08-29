#!/bin/sh
# Container entrypoint.
#
# Three jobs, in this order, because each depends on the previous one:
#   1. Guarantee the data directory exists and is writable. Alembic's env.py
#      resolves the SQLite URL under APPLYUMINATI_DATA_DIR but does not create
#      the directory, so a missing or root-owned /data would fail as an opaque
#      "unable to open database file" instead of an actionable message.
#   2. Bring the schema to head. Doing this in the entrypoint rather than in
#      application startup keeps migrations a single serialised step that fails
#      the container loudly instead of half-starting an API against old tables.
#   3. exec uvicorn, so PID 1 is the server and SIGTERM reaches it directly.
#
# `cli` as the first argument dispatches to the Typer CLI instead, which makes
# `docker run --rm <image> cli doctor` work against the same configuration the
# server would see. That path deliberately skips migrations: `cli --help` must
# never require a database.

set -eu

DATA_DIR="${APPLYUMINATI_DATA_DIR:-/data}"

if ! mkdir -p "${DATA_DIR}" 2>/dev/null; then
    echo "applyuminati: cannot create data directory ${DATA_DIR}." >&2
    echo "applyuminati: mount a writable volume at ${DATA_DIR} (the image runs as uid 10001)." >&2
    exit 1
fi

if [ ! -w "${DATA_DIR}" ]; then
    echo "applyuminati: data directory ${DATA_DIR} is not writable by uid $(id -u)." >&2
    echo "applyuminati: fix the volume ownership, e.g. chown -R 10001:10001 on the host path." >&2
    exit 1
fi

if [ "${1:-}" = "cli" ]; then
    shift
    exec applyuminati "$@"
fi

VERSION="$(python -c 'from importlib.metadata import version; print(version("applyuminati"))' 2>/dev/null || echo 'unknown')"

alembic upgrade head

echo "applyuminati ${VERSION} starting | data_dir=${DATA_DIR} | mode=${APPLYUMINATI_EXECUTION_MODE:-research_only} | http=${APPLYUMINATI_SERVER__HOST:-0.0.0.0}:${APPLYUMINATI_SERVER__PORT:-8000}"

exec python -m uvicorn applyuminati.api.app:app \
    --host "${APPLYUMINATI_SERVER__HOST:-0.0.0.0}" \
    --port "${APPLYUMINATI_SERVER__PORT:-8000}"
