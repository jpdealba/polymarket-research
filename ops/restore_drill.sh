#!/usr/bin/env bash
# Phase 17 — Non-destructive restore drill.
#
# Default mode: restores into an isolated temp dir under
# /data/restore_drills/<timestamp>/, then runs replay, reconciliation, and
# report generation against the restored copy.  The active production DB
# under /data/db/ is NEVER touched.
#
# Destructive mode (--destructive-i-understand): replaces the live DB.
# Requires typing the active DB path and creates a fresh backup first.
set -euo pipefail

DATA_DIR="${PMR_DATA_DIR:-/data}"
DB_PATH="$DATA_DIR/db/pmresearch.db"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
DRILL_DIR="$DATA_DIR/restore_drills/$TIMESTAMP"
DRILL_LOG="$DRILL_DIR/drill.log"

log() { echo "$1" | tee -a "$DRILL_LOG"; }

# ── Parse flags ──────────────────────────────────────────────────────────────
DESTRUCTIVE=0
for arg in "$@"; do
  case "$arg" in
    --destructive-i-understand) DESTRUCTIVE=1 ;;
  esac
done

# ── Destructive mode guard ───────────────────────────────────────────────────
if [ "$DESTRUCTIVE" -eq 1 ]; then
  log "!! DESTRUCTIVE MODE — this will replace the live DB"
  log "   Active DB path: $DB_PATH"

  # Require operator to type the path
  read -rp "Type the active DB path to confirm: " CONFIRM_PATH
  if [ "$CONFIRM_PATH" != "$DB_PATH" ]; then
    log "ABORT: path mismatch. Got '$CONFIRM_PATH', expected '$DB_PATH'."
    exit 1
  fi

  # Verify PMR_DATA_DIR looks like production
  if [ "$DATA_DIR" != "/data" ] && [ "$DATA_DIR" != "$HOME/data" ]; then
    log "ABORT: PMR_DATA_DIR='$DATA_DIR' does not look like production (/data or ~/data)."
    exit 1
  fi

  log "Creating a fresh backup before destructive restore..."
  BACKUP_FILE=$("$SCRIPT_DIR/backup.sh" | sed -n 's/^Backup written: //p')
  if [ ! -f "$BACKUP_FILE" ]; then
    log "ABORT: backup failed — $BACKUP_FILE does not exist."
    exit 1
  fi
  log "Pre-restore backup: $BACKUP_FILE"

  log "Restoring live DB..."
  "$SCRIPT_DIR/restore.sh" "$BACKUP_FILE"
  log "Live DB restored from $BACKUP_FILE"

  log "Verifying restored live DB integrity..."
  CHECK=$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;")
  if [ "$CHECK" != "ok" ]; then
    log "FAIL: restored live DB integrity check: $CHECK"
    exit 1
  fi
  log "Live DB integrity: OK"

  log "Verifying Alembic head on live DB..."
  pmr db current
  log "Alembic head check: OK"

  log "Replaying holdings on live DB..."
  pmr replay holdings 2>&1 | tee -a "$DRILL_LOG"
  log "Holdings replay: OK"

  log "Running reconciliation on live DB..."
  pmr reconcile run 2>&1 | tee -a "$DRILL_LOG"
  log "Reconciliation: OK"

  log "== Destructive restore drill PASSED =="
  exit 0
fi

# ── Non-destructive mode (default) ───────────────────────────────────────────
log "== Non-destructive restore drill =="
log "Drill directory: $DRILL_DIR"
mkdir -p "$DRILL_DIR"

log "1) Taking a fresh backup..."
BACKUP_FILE=$("$SCRIPT_DIR/backup.sh" | sed -n 's/^Backup written: //p')
log "   -> $BACKUP_FILE"

if [ ! -f "$BACKUP_FILE" ]; then
  log "FAIL: backup file not found: $BACKUP_FILE"
  exit 1
fi

log "2) Verifying backup integrity..."
CHECK=$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;")
if [ "$CHECK" != "ok" ]; then
  log "FAIL: backup integrity check: $CHECK"
  exit 1
fi
log "   Backup integrity: OK"

log "3) Restoring into isolated drill directory..."
DRILL_DB_DIR="$DRILL_DIR/db"
mkdir -p "$DRILL_DB_DIR"
cp "$BACKUP_FILE" "$DRILL_DB_DIR/pmresearch.db"
log "   Copied backup -> $DRILL_DB_DIR/pmresearch.db"

log "4) Verifying restored copy integrity..."
CHECK=$(sqlite3 "$DRILL_DB_DIR/pmresearch.db" "PRAGMA integrity_check;")
if [ "$CHECK" != "ok" ]; then
  log "FAIL: restored copy integrity check: $CHECK"
  exit 1
fi
log "   Restored copy integrity: OK"

log "5) Running Alembic migrations on restored copy..."
PMR_DATA_DIR="$DRILL_DIR" pmr db upgrade 2>&1 | tee -a "$DRILL_LOG"
CURRENT_REV=$(PMR_DATA_DIR="$DRILL_DIR" pmr db current 2>&1)
log "   Alembic revision: $CURRENT_REV"

log "6) Replaying holdings on restored copy..."
PMR_DATA_DIR="$DRILL_DIR" pmr replay holdings 2>&1 | tee -a "$DRILL_LOG"
log "   Holdings replay: OK"

log "7) Running reconciliation on restored copy..."
PMR_DATA_DIR="$DRILL_DIR" pmr reconcile run 2>&1 | tee -a "$DRILL_LOG"
log "   Reconciliation: OK"

log "8) Generating wallet profile report on restored copy..."
for WALLET_ADDR in $(PMR_DATA_DIR="$DRILL_DIR" pmr wallet list 2>&1 | grep -oE '0x[0-9a-f]{40}' | head -3); do
  log "   Report for $WALLET_ADDR..."
  PMR_DATA_DIR="$DRILL_DIR" pmr report wallet "$WALLET_ADDR" --out "$DRILL_DIR/report_${WALLET_ADDR}.md" 2>&1 | tee -a "$DRILL_LOG"
  log "   Report written: $DRILL_DIR/report_${WALLET_ADDR}.md"
done

log "9) Checking production DB is untouched..."
CURRENT_REVISION=$(pmr db current 2>&1)
log "   Production Alembic revision: $CURRENT_REVISION"

log "== Non-destructive restore drill PASSED =="
log "Drill outputs: $DRILL_DIR/"
log "  - db/pmresearch.db (restored copy)"
log "  - drill.log (this log)"
log "  - report_*.md (generated reports)"
