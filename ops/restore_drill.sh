#!/usr/bin/env bash
# Scripted drill: backup -> simulate DB loss -> restore -> verify integrity
# and Alembic head. Exits non-zero on any failure.
set -euo pipefail

DATA_DIR="${PMR_DATA_DIR:-/data}"
DB_PATH="$DATA_DIR/db/pmresearch.db"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Restore drill =="

echo "1) Taking a fresh backup..."
BACKUP_FILE=$("$SCRIPT_DIR/backup.sh" | sed -n 's/^Backup written: //p')
echo "   -> $BACKUP_FILE"

echo "2) Simulating DB loss..."
rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"

echo "3) Restoring..."
"$SCRIPT_DIR/restore.sh" "$BACKUP_FILE"

echo "4) Verifying integrity..."
CHECK=$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;")
if [ "$CHECK" != "ok" ]; then
  echo "FAIL: integrity check returned: $CHECK" >&2
  exit 1
fi

echo "5) Verifying Alembic head..."
pmr db current

echo "== Restore drill PASSED =="
