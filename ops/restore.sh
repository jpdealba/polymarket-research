#!/usr/bin/env bash
# Restore a VACUUM INTO backup, replacing the live DB. Verifies integrity
# before touching anything.
set -euo pipefail

DATA_DIR="${PMR_DATA_DIR:-/data}"
DB_PATH="$DATA_DIR/db/pmresearch.db"

if [ $# -ne 1 ]; then
  echo "Usage: restore.sh <backup_file>" >&2
  exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
  echo "Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

CHECK=$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;")
if [ "$CHECK" != "ok" ]; then
  echo "Backup failed integrity check: $CHECK" >&2
  exit 1
fi

mkdir -p "$DATA_DIR/db"
rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"
cp "$BACKUP_FILE" "$DB_PATH"

echo "Restored $BACKUP_FILE -> $DB_PATH"
