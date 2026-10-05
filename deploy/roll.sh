#!/usr/bin/env bash
# Deploy = build an immutable generation and roll the stack to it: `deploy/roll.sh [<rev> [roller args]]`
# (default rev: origin's default branch; e.g. `deploy/roll.sh origin/main --drain-timeout 600`). The host only fetches, builds and launches — the roller
# runs from the NEW image as the one-shot `teatree-roller` service, reading the compose
# topology baked in that image, so nothing it executes comes from this checkout.
# Exit 0 = rolled (or already serving), 3 = rolled back (the previous generation serves),
# 75 = another deploy holds the lock, anything else = the roll could not run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$SCRIPT_DIR/generation-topology.sh"
. "$SCRIPT_DIR/deploy-lock.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
DEPLOY_LOCK="${TEATREE_DEPLOY_LOCK:-/tmp/teatree-deploy.lock}"
RECORD_REFRESH_SECONDS=60
# Another compose project (and its own promoted tag, admin port and lock) is a second stack on the same daemon.
PROJECT="$(generation_compose_project)"

# The watchdog stands back while it sees this record, and it sees the host tmp only as /host-tmp.
HOST_TMP="${TEATREE_HOST_TMP:-/tmp}"
HOST_TMP="${HOST_TMP%/}"
case "$DEPLOY_LOCK" in
"$HOST_TMP"/*) export TEATREE_WATCHDOG_DEPLOY_LOCK="/host-tmp/${DEPLOY_LOCK#"$HOST_TMP"/}" ;;
*)
    echo "roll: $DEPLOY_LOCK is outside TEATREE_HOST_TMP ($HOST_TMP), the only place the watchdog can read the deploy record — keep the lock under it." >&2
    exit 64
    ;;
esac

# The same layout rule as deploy.sh and build-generation.sh; a plain core clone keeps the legacy stack on its teatree_src volume.
PROJECT_ROOT="$REPO_ROOT"
LEGACY_SOURCE_MOUNT=""
if [ "$(basename "$REPO_ROOT")" = teatree ] && [ "$(basename "$(dirname "$REPO_ROOT")")" = vendor ]; then
    LEGACY_SOURCE_MOUNT="$REPO_ROOT"
    if [ -f "$(dirname "$(dirname "$REPO_ROOT")")/pyproject.toml" ]; then
        PROJECT_ROOT="$(dirname "$(dirname "$REPO_ROOT")")"
        LEGACY_SOURCE_MOUNT="$PROJECT_ROOT"
    fi
fi

git -C "$PROJECT_ROOT" fetch --prune origin
TO="$(git -C "$PROJECT_ROOT" rev-parse --verify "${1:-origin/HEAD}^{commit}")"
[ "$#" -eq 0 ] || shift
IMAGE="${TEATREE_IMAGE_REPOSITORY:-teatree-factory}:$TO"

# The grace the roller receives (its own default unless --drain-timeout is passed), which the
# record's deadline and the lock's reclaim age must outlast.
DRAIN_TIMEOUT=1800
previous=""
for arg in "$@"; do
    [ "$previous" != --drain-timeout ] || DRAIN_TIMEOUT="$arg"
    case "$arg" in --drain-timeout=*) DRAIN_TIMEOUT="${arg#--drain-timeout=}" ;; esac
    previous="$arg"
done
case "$DRAIN_TIMEOUT" in
'' | *[!0-9]*)
    echo "roll: --drain-timeout takes whole seconds, got '$DRAIN_TIMEOUT'." >&2
    exit 64
    ;;
esac
# Base 10: a leading zero would otherwise read as octal, and 08 or 09 as an arithmetic error.
DRAIN_TIMEOUT=$((10#$DRAIN_TIMEOUT))

# One convergence at a time, shared with deploy.sh.
DEPLOY_LOCK_DIR=""
DEPLOY_LOCK_MAX_AGE_MINUTES=$(((DRAIN_TIMEOUT + 3600) / 60))
DEPLOY_LOCK_MAX_RECLAIMS=5
if command -v flock >/dev/null 2>&1; then
    exec 9>>"$DEPLOY_LOCK"
    if ! flock -n 9; then
        echo "roll: another deploy holds $DEPLOY_LOCK — retry once it finishes." >&2
        exit 75
    fi
else
    locked=0
    acquire_deploy_lock_dir roll || locked=$?
    case "$locked" in
    0) ;;
    1)
        echo "roll: another deploy (pid $DEPLOY_LOCK_HOLDER) holds $DEPLOY_LOCK_DIR — retry once it finishes." >&2
        exit 75
        ;;
    *) exit 1 ;;
    esac
fi
SCRATCH="$(mktemp -d)"
# The same "<pid> <heartbeat> <deadline>" record deploy.sh keeps, so the watchdog and the doctor
# read a roll as a convergence in flight; the beat stops with this shell and never holds fd 9.
ROLL_DEADLINE=$(($(date -u +%s) + DRAIN_TIMEOUT + 3600))
write_record() { printf '%s %s %s\n' "$$" "$(date -u +%s)" "$ROLL_DEADLINE" >"$DEPLOY_LOCK"; }
write_record
(
    exec 9>&-
    while sleep "$RECORD_REFRESH_SECONDS" && kill -0 "$$" 2>/dev/null; do
        [ ! -s "$DEPLOY_LOCK" ] || write_record || true
        # A live roll's mkdir lock never ages into a reclaim.
        [ -z "$DEPLOY_LOCK_DIR" ] || touch "$DEPLOY_LOCK_DIR" 2>/dev/null || true
    done
) </dev/null >/dev/null 2>&1 &
REFRESHER=$!
release() {
    kill "$REFRESHER" 2>/dev/null || true
    : >"$DEPLOY_LOCK" 2>/dev/null || true
    _release_deploy_lock
    rm -rf "$SCRATCH"
}
trap release EXIT

"$SCRIPT_DIR/build-generation.sh" "$TO"

# The compose interpolation every generation's topology reads, derived as deploy.sh derives it.
export TEATREE_DEPLOY_CHECKOUT="$REPO_ROOT"
export TEATREE_HOST_HOME="${TEATREE_HOST_HOME:-$HOME}"
# dockerd creates a missing bind source root-owned, locking the non-root container out of it.
install -d -m 700 "$TEATREE_HOST_HOME/.password-store" "$TEATREE_HOST_HOME/.gnupg"
install -d "$TEATREE_HOST_HOME/.local/share/teatree" "$TEATREE_HOST_HOME/.local/share/teatree-worktrees" \
    "$TEATREE_HOST_HOME/workspace/t3-workspaces" "$TEATREE_HOST_HOME/.local/share/uv/python" \
    "$TEATREE_HOST_HOME/.local/bin"
export TEATREE_UID="${TEATREE_UID:-$(id -u)}"
# Unset, a container refuses /host-proc: under Docker Desktop it is the VM's table, not this host's.
export TEATREE_HOST_OS="${TEATREE_HOST_OS:-$(uname -s)}"
[ -z "${TEATREE_SOURCE_MOUNT:-$LEGACY_SOURCE_MOUNT}" ] || export TEATREE_SOURCE_MOUNT="${TEATREE_SOURCE_MOUNT:-$LEGACY_SOURCE_MOUNT}"
if [ -z "${TEATREE_DOCKER_SOCKET_GID:-}" ]; then
    TEATREE_DOCKER_SOCKET_GID=0
    if [ "$(uname -s)" = Linux ] && [ -S /var/run/docker.sock ]; then
        TEATREE_DOCKER_SOCKET_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)"
    fi
fi
export TEATREE_DOCKER_SOCKET_GID
for pair in "GITLAB_TOKEN:TEATREE_GITLAB_TOKEN_PASS_PATH" "NOTION_TOKEN:NOTION_TOKEN_PASS_PATH"; do
    var="${pair%%:*}"
    key="${pair#*:}"
    if [ -z "${!var:-}" ] && [ -n "${!key:-}" ] && command -v pass >/dev/null 2>&1; then
        export "$var=$(pass show "${!key}" 2>/dev/null | head -n1 || true)"
    fi
done
# The worker's caps come from the new image's own sizer, reading the daemon's real resources.
if [ -z "${TEATREE_WORKER_CPUS:-}" ] || [ -z "${TEATREE_WORKER_MEM_LIMIT:-}" ]; then
    SIZING="$(docker run --rm --pull never --entrypoint python "$IMAGE" -m teatree.utils.ram_probe compose-sizing)"
    eval "$SIZING"
fi
export TEATREE_WORKER_CPUS TEATREE_WORKER_MEM_LIMIT
echo "roll: $IMAGE — worker cpus=${TEATREE_WORKER_CPUS:-<default>} mem_limit=${TEATREE_WORKER_MEM_LIMIT:-<default>}"

generation_compose_files "$IMAGE" "$SCRATCH"
COMPOSE_ARGS=(-p "$PROJECT" --project-directory "$SCRIPT_DIR" "${GENERATION_COMPOSE_ARGS[@]}")
CONTAINER_HOME="$(generation_container_home "$SCRATCH/docker-compose.yml")"
# The checkout is bind-mounted at its own path, so one at or around the baked tree would shadow it.
BAKED_ROOT="$CONTAINER_HOME/teatree"
case "$REPO_ROOT/" in
"$BAKED_ROOT"/*)
    echo "roll: the deploy checkout $REPO_ROOT sits at or under $BAKED_ROOT, the tree the image bakes — move the checkout elsewhere." >&2
    exit 64
    ;;
esac
case "$BAKED_ROOT/" in
"$REPO_ROOT"/*)
    echo "roll: the deploy checkout $REPO_ROOT contains $BAKED_ROOT, the tree the image bakes — move the checkout elsewhere." >&2
    exit 64
    ;;
esac
# A rollback to the legacy stack runs this checkout's source, whose core a fork nests under vendor/teatree.
LEGACY_CLONE_DIR="$CONTAINER_HOME/teatree"
[ "$PROJECT_ROOT" = "$REPO_ROOT" ] || LEGACY_CLONE_DIR="$LEGACY_CLONE_DIR/vendor/teatree"
export TEATREE_LEGACY_CLONE_DIR="${TEATREE_LEGACY_CLONE_DIR:-$LEGACY_CLONE_DIR}"

ENV_FLAGS=()
for var in TEATREE_DEPLOY_CHECKOUT TEATREE_HOST_HOME TEATREE_HOST_OS TEATREE_UID TEATREE_SOURCE_MOUNT TEATREE_DOCKER_SOCKET_GID \
    TEATREE_WORKER_CPUS TEATREE_WORKER_MEM_LIMIT GITLAB_TOKEN NOTION_TOKEN TEATREE_HOST_TMP TEATREE_DISK_TMPDIR \
    TEATREE_LEGACY_CLONE_DIR TEATREE_WATCHDOG_DEPLOY_LOCK TEATREE_COMPOSE_PROJECT TEATREE_PROMOTED_TAG TEATREE_ADMIN_PORT TEATREE_IMAGE_REPOSITORY; do
    [ -z "${!var:-}" ] || ENV_FLAGS+=(-e "$var")
done

TEATREE_IMAGE="$IMAGE" docker compose "${COMPOSE_ARGS[@]}" run --rm --no-deps ${ENV_FLAGS[@]+"${ENV_FLAGS[@]}"} \
    -e "TEATREE_DEPLOY_LOCK=$TEATREE_WATCHDOG_DEPLOY_LOCK" -e "TEATREE_ROLL_RECORD_PID=$$" \
    teatree-roller deploy roll --to "$TO" "$@"
