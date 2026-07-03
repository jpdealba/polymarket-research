#!/usr/bin/env bash
# VACUUM INTO a timestamped backup; never copy the live WAL-mode file directly.
set -euo pipefail

DATA_DIR="${PMR_DATA_DIR:-/data}"
DB_PATH="$DATA_DIR/db/pmresearch.db"
BACKUP_DIR="$DATA_DIR/backups"
RETAIN="${PMR_BACKUP_RETAIN:-14}"

mkdir -p "$BACKUP_DIR"

TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_FILE="$BACKUP_DIR/pmresearch_${TS}.db"

sqlite3 "$DB_PATH" "VACUUM INTO '$BACKUP_FILE'"

echo "Backup written: $BACKUP_FILE"

# Prune old backups, keep the most recent $RETAIN.
ls -1t "$BACKUP_DIR"/pmresearch_*.db 2>/dev/null | tail -n "+$((RETAIN + 1))" | xargs -r rm -f

if [ -n "${PMR_RCLONE_REMOTE:-}" ]; then
  rclone copy "$BACKUP_FILE" "$PMR_RCLONE_REMOTE"
  echo "Synced to $PMR_RCLONE_REMOTE"
fi
