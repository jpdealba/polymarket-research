# Code only — disposable. All persistent state lives on the mounted /data volume.
FROM python:3.12-slim

# sqlite3 CLI: ops/backup.sh, ops/restore.sh, ops/restore_drill.sh run via
# `docker exec` against this image (VACUUM INTO needs the CLI, not just the
# Python sqlite3 module's API surface used elsewhere in this codebase).
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY pmresearch ./pmresearch
COPY apps ./apps
COPY alembic ./alembic
COPY alembic.ini ./
COPY ops ./ops

# Editable install: keeps `__file__` resolution pointing at /app, so the CLI
# and collector can locate alembic.ini/alembic/ next to the package (see
# pmresearch/db/migrations.py) the same way a local checkout does.
RUN pip install --no-cache-dir -e .

ENV PMR_DATA_DIR=/data

CMD ["python", "apps/collector/main.py"]
