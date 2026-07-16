#!/usr/bin/env bash
# One-time, idempotent migration from Docker named volumes to host bind mounts.
#
# PR-1 of the Docker unification externalized the factory's state onto the host
# at its canonical absolute paths (see deploy/docker-compose.yml). Before that
# change the DB, worktrees, and workspaces lived in Docker named volumes; this
# script copies that real factory state out onto the host bind paths so nothing
# is lost when the stack switches to bind mounts.
#
# SAFETY: the OPERATOR runs this, not the deploy workflow. It refuses to run
# while the stack is up, ARCHIVES the existing host DB + backups (timestamped,
# never deleted) BEFORE overwriting, then copies the volume contents onto the
# host paths and brings the stack up. Per the design the CONTAINER volume holds
# the real factory state, so the host DB is archived-then-overwritten from it.
#
# Reading /var/lib/docker/volumes needs root and `cp -a` must preserve the
# teatree uid/gid, so run this with sudo:
#
#     sudo deploy/migrate-volume-data.sh
#
# Idempotent: safe to re-run — each run makes a fresh timestamped archive and
# re-copies. Because the stack must be DOWN and the host is archived first, a
# re-run can never lose data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# All paths are overridable for testing; the defaults are the real box paths.
COMPOSE_FILE="${MIGRATE_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
DOCKER="${MIGRATE_DOCKER:-docker}"

HOST_DATA="${MIGRATE_HOST_DATA:-/home/teatree/.local/share/teatree}"
HOST_WORKTREES="${MIGRATE_HOST_WORKTREES:-/home/teatree/.local/share/teatree-worktrees}"
HOST_WORKSPACES="${MIGRATE_HOST_WORKSPACES:-/home/teatree/workspace/t3-workspaces}"

VOL_DATA="${MIGRATE_VOL_DATA:-/var/lib/docker/volumes/teatree_teatree_data/_data}"
VOL_WORKTREES="${MIGRATE_VOL_WORKTREES:-/var/lib/docker/volumes/teatree_teatree_worktrees/_data}"
VOL_WORKSPACES="${MIGRATE_VOL_WORKSPACES:-/var/lib/docker/volumes/teatree_teatree_workspaces/_data}"

ARCHIVE_ROOT="${MIGRATE_ARCHIVE_ROOT:-/home/teatree/.local/share}"

step() { printf '==> %s\n' "$*"; }
die() { printf 'migrate-volume-data: %s\n' "$*" >&2; exit 1; }

refuse_if_stack_up() {
    step "Checking the stack is stopped"
    local running
    running="$("$DOCKER" compose -f "$COMPOSE_FILE" ps -q 2>/dev/null || true)"
    if [ -n "$running" ]; then
        die "the stack is up. Stop it first, then re-run:
    $DOCKER compose -f $COMPOSE_FILE down"
    fi
}

require_volumes_readable() {
    step "Checking the Docker volumes are readable"
    local d
    for d in "$VOL_DATA" "$VOL_WORKTREES" "$VOL_WORKSPACES"; do
        [ -d "$d" ] || die "volume dir not found: $d — run with sudo, or the named volumes are already gone (migration already done?)."
        [ -r "$d" ] || die "cannot read $d — run this script with sudo."
    done
}

archive_host_state() {
    # Timestamped, never deleted — the safety net before we overwrite the host DB.
    local ts archive
    ts="$(date +%Y%m%d-%H%M%S)"
    archive="$ARCHIVE_ROOT/teatree-bindmount-archive-$ts"
    step "Archiving the existing host DB + backups -> $archive"
    mkdir -p "$archive"
    local item
    for item in db.sqlite3 db.sqlite3-wal db.sqlite3-shm; do
        [ -e "$HOST_DATA/$item" ] && cp -a "$HOST_DATA/$item" "$archive/"
    done
    [ -d "$HOST_DATA/backups" ] && cp -a "$HOST_DATA/backups" "$archive/"
    printf 'archived host state to %s\n' "$archive"
}

copy_volume_to_host() {
    # KEEP the container volume's data (the real factory state) and lay it down on
    # the host bind paths. `cp -a` preserves the teatree uid/gid so the container
    # user can still read/write after the switch. Trailing /. copies dotfiles too
    # (.password-store, .gnupg, instance_id, backups/).
    step "Copying volume DB + credentials + backups -> $HOST_DATA"
    mkdir -p "$HOST_DATA"
    cp -a "$VOL_DATA/." "$HOST_DATA/"

    step "Copying volume worktrees -> $HOST_WORKTREES"
    mkdir -p "$HOST_WORKTREES"
    cp -a "$VOL_WORKTREES/." "$HOST_WORKTREES/"

    step "Copying volume workspaces -> $HOST_WORKSPACES"
    mkdir -p "$HOST_WORKSPACES"
    cp -a "$VOL_WORKSPACES/." "$HOST_WORKSPACES/"
}

bring_stack_up() {
    step "Bringing the stack up on the host bind mounts"
    "$DOCKER" compose -f "$COMPOSE_FILE" up -d
}

main() {
    refuse_if_stack_up
    require_volumes_readable
    archive_host_state
    copy_volume_to_host
    bring_stack_up
    step "Done. The factory state now lives on the host bind paths."
}

main "$@"
