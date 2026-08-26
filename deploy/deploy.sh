#!/usr/bin/env bash
# Converge the teatree headless stack on the box. Idempotent: re-running brings
# the checkout current, rebuilds the image, and re-applies the compose stack.
# Run as the deploy user (in the docker group) from the repo checkout.
# Reads NO secrets — compose's env_file (deploy/teatree.env) supplies them.
set -euo pipefail

# PHYSICAL resolution (`pwd -P`), matching the sibling `t3`. A fork commonly exposes
# this directory through a repo-root `deploy -> vendor/teatree/deploy` symlink, and
# bash resolves `..` LOGICALLY: invoked as `deploy/deploy.sh`, plain `pwd` yields
# `<fork>/deploy`, whose `..` is the FORK ROOT rather than `<fork>/vendor/teatree`.
# The `git fetch --prune origin` + `git pull --ff-only` below would then run against
# the fork on whatever branch is checked out. With no symlink involved `pwd -P` is
# identical to `pwd`, so a standalone clone is unaffected.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
HOST_IDENTITY_FILE="$SCRIPT_DIR/docker-compose.host-identity.yml"
ENV_FILE="$SCRIPT_DIR/teatree.env"

# The container's fixed HOME, which every mount TARGET in docker-compose.yml is
# expressed under. When the host home differs, host and container disagree about what
# a worktree path means — and `t3 <overlay> worktree start` hands the daemon worktree
# paths it resolved as CONTAINER paths, which the daemon then rejects with "mounts
# denied: the path ... is not shared from the host". The overlay file adds a second,
# host-identical view of the worktree tree so one coordinate satisfies both venues.
# On a host whose home already IS this path the two mount targets would collide, so
# the overlay is added only when they differ. On the box they always match, so nothing
# about the box changes.
# READ from the compose file that declares it rather than restated here, so the two
# can never drift: the clones volume TARGET is `<container home>/workspace` by
# construction, which makes it the canonical statement of that home.
CONTAINER_HOME="$(sed -n 's|^[[:space:]]*-[[:space:]]*teatree_clones:\(.*\)/workspace[[:space:]]*$|\1|p' "$COMPOSE_FILE" | head -n1)"

# Composed at CALL time rather than once at source time: `TEATREE_HOST_HOME` is
# exported further down, beside the directory pre-creation it must agree with, so the
# functions defined above that point would otherwise capture it unset.
compose() {
    if [ "${TEATREE_HOST_HOME:-$CONTAINER_HOME}" = "$CONTAINER_HOME" ]; then
        docker compose -f "$COMPOSE_FILE" "$@"
    else
        docker compose -f "$COMPOSE_FILE" -f "$HOST_IDENTITY_FILE" "$@"
    fi
}

# Single-convergence invariant (host flock). GitHub's `concurrency: deploy` group
# serializes the WORKFLOW, but a remote deploy.sh can outlive its GitHub job — an
# SSH drop does not kill the remote process, and the drain can run longer than the
# job's timeout — so two runs could otherwise converge on the box at once. Two
# overlapping worker drains each set `worker_quiescing` ON, and a lingering older
# drain re-asserts it AFTER a newer run's fresh init cleared it, stranding
# admission OFF indefinitely (the worker then admits ZERO new coding/planning
# tasks — they pile up and dead-letter). A host flock guarantees exactly one
# convergence at a time; a second invocation exits cleanly, since the holder always
# fast-forwards to the latest main and GitHub re-fires for any later push.
DEPLOY_LOCK="${TEATREE_DEPLOY_LOCK:-/tmp/teatree-deploy.lock}"
# Append, never truncate: `>` would wipe the winner's in-progress record below from an
# invocation that goes on to lose the race.
exec 9>>"$DEPLOY_LOCK"
if ! flock -n 9; then
    echo "deploy: another convergence already holds $DEPLOY_LOCK — exiting (it converges to latest main)." >&2
    exit 0
fi

# The convergence's own in-progress record (#4339). /proc/locks is filtered by pid
# namespace, so the flock above is invisible from the watchdog CONTAINER; this record is
# what crosses that boundary, and a crash loop cannot write it. Cleared on exit, so a
# lock file outliving its holder reads as not held.
printf '%s %s\n' "$$" "$(date -u +%s)" >"$DEPLOY_LOCK"

# Fail-safe against a stranded quiescing gate. If this run drains the worker (which
# sets `worker_quiescing` ON) but then exits BEFORE the swap that would recreate the
# worker and clear the gate (a mid-deploy failure under `set -e`), the still-live
# worker would stay quiesced forever. The EXIT trap clears the gate so admission
# resumes. A no-op after a successful resume and when no drain ran; best-effort,
# never fails the run. Safe under the flock: no other convergence owns the gate.
#
# WHICH WAY TO FAIL depends on which worker is live and what schema it faces. init
# migrates the control DB at stage 3, so a strand between it and the worker swap
# leaves PRE-migration code live against the NEW schema — the one window where
# re-opening admission is worse than leaving it shut, because it hands that worker
# fresh work to run against a database it does not match. A stall is visible and one
# command from recovery; a mismatched claim is neither. So the clear is withheld
# there and taken everywhere else, including after the swap, where the live worker is
# the fresh one and the clear is `resume_admission`'s retry.
_DRAINED=false
_SWAP_DONE=false
_INIT_RAN=false
_WORKER_SWAPPED=false
# Clears the in-progress record above (#4339). The `if` guards `${DEPLOY_LOCK:-}`
# rather than a bare `$DEPLOY_LOCK` so this stays a safe no-op wherever the var is
# unset — e.g. this fail-safe block lifted verbatim into a test harness that never
# declared it — and an `if` condition (unlike a bare `&&` list) is exempt from
# `set -e` under this script's own `set -euo pipefail`.
_release_deploy_record() {
    if [ -n "${DEPLOY_LOCK:-}" ]; then
        : >"$DEPLOY_LOCK" 2>/dev/null || true
    fi
}
_clear_quiescing_if_stranded() {
    if [ "$_DRAINED" = true ] && [ "$_SWAP_DONE" = false ]; then
        # Say "cleanup", loudly. This runs LAST, so it is the final line in the
        # Action log and reads as the cause to anyone triaging the failure — it is
        # not; the real error is further up. One triage of this run was nearly
        # anchored on this very line.
        if [ "$_INIT_RAN" = true ] && [ "$_WORKER_SWAPPED" = false ]; then
            echo "deploy: [cleanup, NOT the failure cause — see the error above] init already migrated the control DB and the worker was never swapped, so the live worker runs pre-migration code against the new schema." >&2
            echo "        Leaving worker_quiescing ON: the box admits no new work until a convergence completes. Re-run the deploy, or resume deliberately with 't3 teatree config_setting set worker_quiescing false'." >&2
            return
        fi
        echo "deploy: [cleanup, NOT the failure cause — see the error above] exiting after a drain but before the swap; clearing worker_quiescing so admission resumes." >&2
        compose exec -T teatree-worker \
            t3 teatree config_setting set worker_quiescing false >/dev/null 2>&1 || true
    fi
}
trap '_clear_quiescing_if_stranded; _release_deploy_record' EXIT

# The admin can serve while the worker crash-loops, so a converged deploy must
# confirm the worker process itself is running.
worker_running() {
    if compose exec -T teatree-worker t3 worker status --json 2>/dev/null \
        | grep -q '"running"[[:space:]]*:[[:space:]]*true'; then
        return 0
    fi
    # Fallback when the exec itself fails: a healthy worker is running, no restarts.
    local cid state
    cid="$(compose ps -q teatree-worker 2>/dev/null || true)"
    [ -n "$cid" ] || return 1
    state="$(docker inspect -f '{{.State.Status}}/{{.RestartCount}}' "$cid" 2>/dev/null || true)"
    [ "$state" = "running/0" ]
}

# Docker installed + enabled on boot (so the stack autostarts after a reboot,
# alongside the compose restart policies). is-active needs no root, so the
# common case (docker already running) never invokes sudo.
if ! command -v docker >/dev/null 2>&1; then
    echo "deploy: docker is not installed — see deploy/README.md bootstrap." >&2
    exit 1
fi
if ! systemctl is-active --quiet docker; then
    if ! sudo -n true 2>/dev/null; then
        echo "deploy: docker is not running and passwordless sudo is unavailable — enable it once per deploy/README.md bootstrap (systemctl enable --now docker)." >&2
        exit 1
    fi
    sudo systemctl enable --now docker
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "deploy: missing $ENV_FILE (the deploy workflow writes it before this runs)." >&2
    exit 1
fi

# Bring the build context current (fast-forward only — never clobber local work).
# The helper runs `fetch --prune origin` then `pull --ff-only`, and between them
# reconciles the one class of local dirt a fast-forward provably cannot lose: a
# path whose working-tree bytes already equal the target's. Everything else is
# retained and, if it blocks the merge, named in a fatal diagnostic. See its
# header for the wedge this closes — a `uv.lock` silently re-locked by `uv run`
# aborted every deploy and left the box 42 commits behind, unreported, for days.
bash "$SCRIPT_DIR/fast-forward-checkout.sh" "$REPO_ROOT"
echo "deploy: deploying $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO_ROOT" rev-parse --short HEAD)"

# EVERY host bind-mount SOURCE dir (compose x-teatree-common `volumes:`) must
# pre-exist owned by the deploy user. A missing source is auto-created by dockerd
# ROOT-owned, which locks the non-root container — whose UID must equal this
# deploy user (see deploy/README.md § UID invariant) — out of that mount: the
# credential plane then blocks `pass insert` provisioning, and the data + session
# planes block the DB, worktree, workspace, and transcript writes so `init`
# crash-loops on its first write. Empty dirs are the sane degradation for an
# env-token box (init's preflight then falls through to CLAUDE_CODE_OAUTH_TOKEN).
#
# The credential plane (pass store + its GPG home) is mode 700; the data and
# session planes take the default mode.
#
# `$HOME` here IS the compose sources' host root: the mounts read it as
# `${TEATREE_HOST_HOME:-/home/teatree}`, exported below so the dirs created here
# and the dirs mounted are the same ones by construction. On the box the deploy
# user's home is `/home/teatree`, so this equals the compose default and every
# mount keeps path identity with the container's `/home/teatree/...` targets.
export TEATREE_HOST_HOME="$HOME"

# The checkout this deploy runs out of — the directory holding `deploy/`. The
# watchdog bind-mounts it read-only at PATH IDENTITY and the entrypoint execs
# `$TEATREE_DEPLOY_CHECKOUT/deploy/watchdog.sh` from it, so both sides read this
# one value. On the box `$REPO_ROOT` IS `/home/teatree/teatree-deploy`, the
# compose default, so the box is byte-identical to before.
export TEATREE_DEPLOY_CHECKOUT="$REPO_ROOT"

# Run the WORKING TREE this deploy was invoked from when that tree vendors core —
# the fork layout `<fork>/vendor/teatree/deploy/deploy.sh`. Without this a deploy
# from a fork leaves `${TEATREE_SOURCE_MOUNT:-teatree_src}` on the named volume,
# so the stack runs PUBLIC upstream core: `HOST_ROOT` is empty, entrypoint.sh's
# `--with-editable "$HOST_ROOT"` never fires, no `teatree.overlays` entry point is
# registered, and every headless task on an overlay ticket dies at dispatch
# ("Overlay '<name>' not found"). deploy/t3 already derives this for one-off CLI
# runs; the STACK needs the same wiring or the two disagree about what is deployed.
#
# What gets mounted is the FORK ROOT, not the vendored core alone: entrypoint.sh
# detects a host project by `$TEATREE_CLONE_DIR` ending in `/vendor/teatree` with a
# `pyproject.toml` at its parent, so core must sit one level DOWN from the mount.
# Kept in sync with deploy/t3 by tests/test_deploy_host_project_source_mount.py.
# An operator-set value always wins. Derived from the container HOME read above, so
# the source mount target is never a second copy of that path.
CONTAINER_SOURCE_DIR="$CONTAINER_HOME/teatree"
if [ -z "${TEATREE_SOURCE_MOUNT:-}" ] &&
    [ "$(basename "$REPO_ROOT")" = teatree ] &&
    [ "$(basename "$(dirname "$REPO_ROOT")")" = vendor ]; then
    HOST_PROJECT_ROOT="$(dirname "$(dirname "$REPO_ROOT")")"
    if [ -f "$HOST_PROJECT_ROOT/pyproject.toml" ]; then
        export TEATREE_SOURCE_MOUNT="$HOST_PROJECT_ROOT"
        export TEATREE_CLONE_DIR="${TEATREE_CLONE_DIR:-$CONTAINER_SOURCE_DIR/vendor/teatree}"
    else
        export TEATREE_SOURCE_MOUNT="$REPO_ROOT"
    fi
fi

# TEATREE_DOCKER_SOCKET_GID — the group owning the docker socket AS THE CONTAINER
# SEES IT, which is what docker-compose.yml's `group_add` on teatree-worker reads.
# The worker runs as the non-root TEATREE_UID and needs the daemon for `worktree
# provision` (docker build) and `worktree start` (compose up); the socket is mode
# 0660 root-owned, so without a matching group the mount grants nothing.
#
# NOT always a `stat` of the host socket. On Linux — the box — the daemon shares
# the host kernel, so the host's socket IS the container's and its gid transfers
# (on Debian and Ubuntu the `docker` group, not 0). Under Docker Desktop the
# socket is served from the product's own Linux VM where it is root:root — gid 0 —
# while the host-side node is an ordinary user-owned file whose gid means nothing
# inside the container, so reading the host there would export a group the
# container does not have. Duplicated verbatim in deploy/t3, which is copied and
# run standalone; tests/test_deploy_docker_socket_access.py pins them in sync.
# An operator-set value always wins.
if [ -z "${TEATREE_DOCKER_SOCKET_GID:-}" ]; then
    TEATREE_DOCKER_SOCKET_GID=0
    if [ "$(uname -s)" = Linux ] && [ -S /var/run/docker.sock ]; then
        TEATREE_DOCKER_SOCKET_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)"
    fi
fi
export TEATREE_DOCKER_SOCKET_GID

install -d -m 700 "$HOME/.password-store" "$HOME/.gnupg"
install -d \
    "$HOME/.local/share/teatree" \
    "$HOME/.local/share/teatree-worktrees" \
    "$HOME/workspace/t3-workspaces" \
    "$HOME/.local/share/uv/python" \
    "$HOME/.claude/projects"

# Derive the container's runtime UID from the HOST at deploy time (#3438). Every
# bind mount above is at path identity, so the container's teatree user MUST hold
# the same UID as the host deploy user or every mount is unwritable and init
# crash-loops (see deploy/README.md § UID invariant). deploy.sh runs AS the deploy
# user, so its own UID is the source of truth; it is passed to the image build
# (compose reads ${TEATREE_UID} into the TEATREE_UID build arg). Falls back to the
# owner of the pre-existing data dir, then to 1001 (the live box's deploy user, and
# the Dockerfile's default) if both are somehow unreadable. This keeps a rebuild on
# THIS box at 1001 (no breakage, no chown) and a fresh box at its own deploy UID.
TEATREE_UID="$(id -u 2>/dev/null || true)"
[ -n "$TEATREE_UID" ] || TEATREE_UID="$(stat -c %u "$HOME/.local/share/teatree" 2>/dev/null || true)"
[ -n "$TEATREE_UID" ] || TEATREE_UID=1001
export TEATREE_UID
echo "deploy: container UID (host deploy user) — TEATREE_UID=$TEATREE_UID"

# Derive the worker container's compose CPU/RAM caps from the REAL host at deploy
# time (#3432). deploy.sh runs UNCAPPED on the host, so ram_probe reads true host
# cores/RAM; the worker's cgroup cap then reflects the host, and inside it
# `available_cpu_count` derives concurrency from the host instead of a baked-in
# 3-core cap that made host-derived concurrency a no-op. python3 is present on the
# box; if it is somehow absent, or RAM is unreadable, the vars stay empty and
# compose falls back to its in-file defaults (${TEATREE_WORKER_CPUS:-3.0} /
# ${TEATREE_WORKER_MEM_LIMIT:-18g}). The watchdog's `up -d --no-recreate` does not
# export these, but --no-recreate never re-sizes a running worker; the next deploy
# re-asserts them.
TEATREE_WORKER_CPUS="${TEATREE_WORKER_CPUS:-}"
TEATREE_WORKER_MEM_LIMIT="${TEATREE_WORKER_MEM_LIMIT:-}"
if command -v python3 >/dev/null 2>&1; then
    eval "$(python3 "$REPO_ROOT/src/teatree/utils/ram_probe.py" compose-sizing 2>/dev/null || true)"
fi
export TEATREE_WORKER_CPUS TEATREE_WORKER_MEM_LIMIT
echo "deploy: worker sizing — cpus=${TEATREE_WORKER_CPUS:-<default>} mem_limit=${TEATREE_WORKER_MEM_LIMIT:-<default>}"

# Services the staged swap names, in the order it stages them. Everything compose
# declares beyond this list is converged last, so a service added later is never
# orphaned by the staging.
STAGED_SERVICES="teatree-init teatree-admin teatree-worker teatree-slack-listener"
ADMIN_PROBE_URL="${TEATREE_ADMIN_PROBE_URL:-http://127.0.0.1:8000/admin/login/}"
# The dashboard's own container swap is the ONE window the staging does not remove.
# Exceeding this bound stops the convergence rather than continuing blind, which is
# what keeps the residual unavailability a stated number instead of an implicit one.
ADMIN_SWAP_BUDGET="${TEATREE_ADMIN_SWAP_BUDGET:-300}"
INIT_WAIT_TIMEOUT="${TEATREE_INIT_WAIT_TIMEOUT:-1800}"
RESUME_TIMEOUT="${TEATREE_RESUME_TIMEOUT:-300}"
LOG_ARCHIVE_DIR="${TEATREE_DEPLOY_LOG_ARCHIVE_DIR:-$HOME/.local/share/teatree/deploy-logs}"
LOG_ARCHIVE_KEEP="${TEATREE_DEPLOY_LOG_ARCHIVE_KEEP:-200}"

admin_answers() { curl -fsS -o /dev/null --max-time 5 "$ADMIN_PROBE_URL"; }

# A recreate destroys the container object and its json-file log with it — which is
# how a live diagnosis lost the very worker ticks it was reading. Copy each service's
# log to the bind-mounted data dir before recreating it, so a post-incident read
# spans the swap. Best-effort throughout: losing an archive must not fail a deploy.
archive_service_logs() {
    local svc stamp
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    install -d "$LOG_ARCHIVE_DIR" 2>/dev/null || return 0
    for svc in "$@"; do
        compose logs --no-color --timestamps --tail "${TEATREE_DEPLOY_LOG_ARCHIVE_LINES:-5000}" "$svc" \
            >|"$LOG_ARCHIVE_DIR/$svc-$stamp.log" 2>/dev/null || true
    done
    { ls -1t "$LOG_ARCHIVE_DIR"/*.log 2>/dev/null || true; } |
        tail -n "+$((LOG_ARCHIVE_KEEP + 1))" | xargs -r rm -f --
}

# The one-shot init's state as "<status> <exit-code>" (e.g. "exited 0"), empty when
# unreadable. The first line is taken by expansion, never `| head`: under `pipefail`
# a reader closing the pipe early kills the writer with SIGPIPE and fails the whole
# assignment.
init_state() {
    local ids cid
    ids="$(compose ps --all --quiet teatree-init 2>/dev/null || true)"
    cid="${ids%%$'\n'*}"
    [ -n "$cid" ] || return 0
    docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$cid" 2>/dev/null || true
}

wait_for_init() {
    local deadline=$((SECONDS + INIT_WAIT_TIMEOUT)) state
    while [ "$SECONDS" -lt "$deadline" ]; do
        state="$(init_state)"
        case "$state" in
        "exited 0") return 0 ;;
        exited*)
            echo "deploy: FATAL — teatree-init $state. No app service was recreated, so the previous generation is still serving." >&2
            compose logs --tail 200 teatree-init >&2 || true
            return 1
            ;;
        esac
        sleep 2
    done
    echo "deploy: FATAL — teatree-init did not finish within ${INIT_WAIT_TIMEOUT}s." >&2
    return 1
}

# Quiesce the RUNNING worker: `t3 worker drain` sets the `worker_quiescing` admission
# gate (the claim path then admits ZERO new work) and waits up to
# TEATREE_DRAIN_TIMEOUT seconds for every live CLAIMED lease to finish. The
# supervisor is never stopped, so in-flight sub-agents keep renewing and complete. On
# a grace overrun the drain exits non-zero (code 3); we still PROCEED — a stuck task
# re-queues PENDING via its lease lapse and the fresh worker picks it up.
drain_worker() {
    worker_running || return 0
    echo "deploy: draining teatree-worker (up to ${TEATREE_DRAIN_TIMEOUT:-1800}s for in-flight agents to finish) ..."
    _DRAINED=true
    compose exec -T teatree-worker \
        t3 worker drain --timeout "${TEATREE_DRAIN_TIMEOUT:-1800}" ||
        echo "deploy: drain window exceeded — proceeding (a stuck task re-queues via its lease lapse)"
}

# init's own clear runs BEFORE this convergence quiesces the worker, so nothing else
# re-opens admission on the fresh one — this does. Never fatal: the stack is up
# either way, and the stranded-gate EXIT trap re-attempts it.
resume_admission() {
    local deadline=$((SECONDS + RESUME_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if compose exec -T teatree-worker \
            t3 teatree config_setting set worker_quiescing false >/dev/null 2>&1; then
            _SWAP_DONE=true
            return 0
        fi
        sleep 5
    done
    echo "deploy: WARNING could not clear worker_quiescing within ${RESUME_TIMEOUT}s — the fresh worker may admit no new work. Clear it with 't3 teatree config_setting set worker_quiescing false'." >&2
    return 0
}

# The dashboard moves ALONE, with the worker still answering as the live control-DB
# route. A box where nothing was answering has no continuity to keep, so the gap
# accounting is skipped there and the convergence check at the end owns the wait.
swap_admin() {
    local was_up=false started
    if admin_answers; then was_up=true; fi
    archive_service_logs teatree-admin
    started=$SECONDS
    compose up -d --no-deps teatree-admin || return 1
    if [ "$was_up" != true ]; then
        echo "deploy: no dashboard was answering before the swap — nothing to keep continuous."
        return 0
    fi
    while [ "$((SECONDS - started))" -lt "$ADMIN_SWAP_BUDGET" ]; do
        if admin_answers; then
            echo "deploy: dashboard unavailable for at most $((SECONDS - started))s (bound ${ADMIN_SWAP_BUDGET}s); the worker answered throughout."
            return 0
        fi
        sleep 1
    done
    echo "deploy: FATAL — the dashboard did not answer within ${ADMIN_SWAP_BUDGET}s of its swap; stopping before the worker is touched, so one control-DB route stays up." >&2
    return 1
}

# Every compose service the stages do not name. init is excluded by being staged: a
# plain `up -d` STARTS an exited one-shot, replaying the whole ~minute init.
remaining_services() {
    local svc
    { compose config --services 2>/dev/null || true; } | while IFS= read -r svc; do
        case " $STAGED_SERVICES " in
        *" $svc "*) ;;
        *) printf '%s\n' "$svc" ;;
        esac
    done
}

# Stage the convergence so the control plane is never wholly absent (#4214). One
# all-at-once recreate replaces all five services together: the dashboard and the
# only CLI route vanish for the whole init window (~67s measured), every `t3` call
# inside it fails the way a real outage does, and the container logs a live
# diagnosis was reading are destroyed. Each stage below leaves at least one of
# {teatree-admin, teatree-worker} answering.
staged_swap() {
    # Build first and recreate nothing: the longest phase of a convergence now runs
    # against a fully live stack, and a build failure costs no availability at all.
    compose build || return 1

    drain_worker

    archive_service_logs teatree-init
    compose up -d --no-deps teatree-init || return 1
    wait_for_init || return 1
    _INIT_RAN=true

    # init clears worker_quiescing as its last act, so re-assert the gate: without
    # this the still-live old worker resumes admission and can claim — then lose —
    # a task in the seconds before it is swapped.
    drain_worker

    swap_admin || return 1

    archive_service_logs teatree-worker teatree-slack-listener
    compose up -d --no-deps teatree-worker teatree-slack-listener || return 1
    _WORKER_SWAPPED=true
    resume_admission

    local rest
    rest="$(remaining_services)"
    [ -n "$rest" ] || return 0
    # shellcheck disable=SC2086 # a service list has to word-split into separate args
    archive_service_logs $rest
    # shellcheck disable=SC2086 # same
    compose up -d --no-deps $rest || return 1
}

# Surface the WHY on a build/up failure — `set -e` would otherwise exit before
# the Action log sees anything but "exited (1)". The banner is load-bearing: the
# 200 stale container lines below push the real buildkit error hundreds of lines
# up the Action log, where it reads as unrelated noise. Name the failing step at
# the top of the dump so triage starts in the right place.
staged_swap || {
    echo "deploy: FATAL — the staged convergence failed. The cause is the buildkit/compose error ABOVE this line; what follows is stack state for context, not the failure."
    compose ps
    compose logs --tail 200 teatree-init teatree-worker teatree-admin
    exit 1
} >&2

# Wait for the admin dev server on the box loopback (init clone + install can
# take a few minutes on first run).
echo "deploy: waiting for the admin service on 127.0.0.1:8000 ..."
admin_up=false
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "http://127.0.0.1:8000/admin/login/"; then
        admin_up=true
        break
    fi
    sleep 10
done

if [ "$admin_up" = true ] && worker_running; then
    echo "deploy: admin + worker are up; stack converged."
    exit 0
fi

echo "deploy: convergence check failed — recent logs:" >&2
compose ps >&2 || true
compose logs --tail 50 teatree-init teatree-worker teatree-admin >&2 || true
exit 1
