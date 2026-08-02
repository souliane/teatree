#!/usr/bin/env bash
# Move the canonical control DB off the host bind mount and into the control-DB
# named volume, verifying the copy is sound before anything is asked to trust it.
#
# Runs INSIDE a container, because that is the only place both filesystems are
# visible at once — the data dir is a host bind mount and the volume has no host
# path at all. The host invocation is a one-off container on the stack's own mounts:
#
#     docker compose -f deploy/docker-compose.yml run --rm --no-deps \
#         --entrypoint deploy/migrate-control-db-to-volume.sh teatree-worker
#
# Stop the stack first. The copy is an online `sqlite3 .backup` (never `cp`: copying
# a live SQLite file is how the unrestorable backups in this data dir were produced),
# but the per-table row-count equality asserted below only holds against a quiescent
# source, and that assertion is the point — a mismatch must mean a bad copy, not a
# concurrent write.
#
# Idempotent-by-refusal: an existing database in the target is left untouched unless
# MIGRATE_FORCE=1, so a re-run can never silently overwrite the live control DB.
set -euo pipefail

SOURCE_DB="${MIGRATE_SOURCE_DB:-$HOME/.local/share/teatree/db.sqlite3}"
TARGET_DIR="${MIGRATE_TARGET_DIR:-${T3_CONTROL_DB_DIR:-/var/lib/teatree/control-db}}"
TARGET_DB="$TARGET_DIR/db.sqlite3"

step() { printf '==> %s\n' "$*"; }
die() {
    printf 'migrate-control-db-to-volume: %s\n' "$*" >&2
    exit 1
}

# Read-only URI open: the source is never opened read-write by this script, so it
# stays legal even where the containerized stack owns the file (teatree.db.boundary).
source_uri() { printf 'file:%s?mode=ro' "$SOURCE_DB"; }

# One `<table> <count>` line per user table, table-ordered, so the two sides are
# comparable with a plain diff and the diff NAMES the table that lost rows.
row_counts() {
    local db="$1" table
    while IFS= read -r table; do
        printf '%s %s\n' "$table" "$(sqlite3 "$db" "SELECT count(*) FROM \"$table\";")"
    done < <(sqlite3 "$db" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;")
}

preflight() {
    command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is not on PATH — run this inside the container."
    [ -f "$SOURCE_DB" ] || die "no source database at $SOURCE_DB"
    mkdir -p "$TARGET_DIR" || die "cannot create $TARGET_DIR — is the control-DB volume mounted?"
    [ -w "$TARGET_DIR" ] || die "$TARGET_DIR is not writable by $(id -un) — check the volume's ownership."
    if [ -e "$TARGET_DB" ] && [ "${MIGRATE_FORCE:-0}" != "1" ]; then
        die "$TARGET_DB already exists. The migration has run. Re-run with MIGRATE_FORCE=1 only if you mean to replace it."
    fi
}

copy_online() {
    step "Copying $SOURCE_DB -> $TARGET_DB (online sqlite3 .backup)"
    rm -f "$TARGET_DB" "$TARGET_DB-wal" "$TARGET_DB-shm" "$TARGET_DB-journal"
    sqlite3 "$(source_uri)" ".backup '$TARGET_DB'" || die "the .backup failed; $TARGET_DB is not usable."
}

verify_structure() {
    step "PRAGMA integrity_check on the copy"
    local integrity
    integrity="$(sqlite3 "$TARGET_DB" 'PRAGMA integrity_check;')"
    [ "$integrity" = "ok" ] || die "integrity_check FAILED on $TARGET_DB: $integrity"
    printf '    integrity_check: ok\n'

    step "PRAGMA foreign_key_check on the copy"
    local violations
    violations="$(sqlite3 "$TARGET_DB" 'PRAGMA foreign_key_check;')"
    [ -z "$violations" ] || die "foreign_key_check reported violations on $TARGET_DB:
$violations"
    printf '    foreign_key_check: clean\n'
}

verify_row_counts() {
    step "Comparing per-table row counts"
    local before after
    before="$(row_counts "$(source_uri)")"
    after="$(row_counts "$TARGET_DB")"
    if [ "$before" != "$after" ]; then
        die "per-table row counts differ between source and copy:
$(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") || true)"
    fi
    printf '    %s tables, identical row counts on both sides\n' "$(printf '%s\n' "$before" | wc -l | tr -d ' ')"
}

main() {
    preflight
    copy_online
    verify_structure
    verify_row_counts
    step "Done. The control DB now lives at $TARGET_DB."
    printf 'The source at %s is left in place; archive it once the stack is verified.\n' "$SOURCE_DB"
}

main "$@"
