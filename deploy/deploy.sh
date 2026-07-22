#!/usr/bin/env bash
# Converge the teatree headless stack on the box. Idempotent: re-running brings
# the checkout current, rebuilds the image, and re-applies the compose stack.
# Run as the deploy user (in the docker group) from the repo checkout.
# Reads NO secrets — compose's env_file (deploy/teatree.env) supplies them.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
ENV_FILE="$SCRIPT_DIR/teatree.env"

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
exec 9>"$DEPLOY_LOCK"
if ! flock -n 9; then
    echo "deploy: another convergence already holds $DEPLOY_LOCK — exiting (it converges to latest main)." >&2
    exit 0
fi

# Fail-safe against a stranded quiescing gate. If this run drains the worker (which
# sets `worker_quiescing` ON) but then exits BEFORE the image swap that would
# recreate the worker and clear the gate via its init (a mid-deploy failure under
# `set -e`), the still-live OLD worker would stay quiesced forever. The EXIT trap
# clears the gate so admission resumes. A no-op after a successful swap (the fresh
# init already cleared it) and when no drain ran; best-effort, never fails the run.
# Safe under the flock: no other convergence is running to own the gate.
_DRAINED=false
_SWAP_DONE=false
_clear_quiescing_if_stranded() {
    if [ "$_DRAINED" = true ] && [ "$_SWAP_DONE" = false ]; then
        echo "deploy: exiting after a drain but before the swap — clearing worker_quiescing so admission resumes." >&2
        docker compose -f "$COMPOSE_FILE" exec -T teatree-worker \
            t3 teatree config_setting set worker_quiescing false >/dev/null 2>&1 || true
    fi
}
trap _clear_quiescing_if_stranded EXIT

# The admin can serve while the worker crash-loops, so a converged deploy must
# confirm the worker process itself is running.
worker_running() {
    if docker compose -f "$COMPOSE_FILE" exec -T teatree-worker t3 worker status --json 2>/dev/null \
        | grep -q '"running"[[:space:]]*:[[:space:]]*true'; then
        return 0
    fi
    # Fallback when the exec itself fails: a healthy worker is running, no restarts.
    local cid state
    cid="$(docker compose -f "$COMPOSE_FILE" ps -q teatree-worker 2>/dev/null || true)"
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
git -C "$REPO_ROOT" fetch --prune origin
git -C "$REPO_ROOT" pull --ff-only
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
install -d -m 700 "$HOME/.password-store" "$HOME/.gnupg"
install -d \
    "$HOME/.local/share/teatree" \
    "$HOME/.local/share/teatree-worktrees" \
    "$HOME/workspace/t3-workspaces" \
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

# Drain-then-deploy (rolling / zero-downtime): a deploy must NEVER kill an
# in-flight agent. Before swapping the worker image, quiesce the RUNNING worker —
# `t3 worker drain` sets the `worker_quiescing` admission gate (the claim path then
# admits ZERO new work) and waits up to TEATREE_DRAIN_TIMEOUT seconds for every
# live CLAIMED lease to finish. The supervisor is never stopped, so in-flight
# sub-agents keep renewing and complete. On a grace overrun the drain exits non-zero
# (code 3); we still PROCEED — a stuck task re-queues PENDING via its lease lapse and
# the fresh worker picks it up. The fresh worker's init clears worker_quiescing so
# admission resumes. Skipped when no worker is running (nothing to drain).
if worker_running; then
    echo "deploy: draining teatree-worker (up to ${TEATREE_DRAIN_TIMEOUT:-1800}s for in-flight agents to finish) ..."
    _DRAINED=true
    docker compose -f "$COMPOSE_FILE" exec -T teatree-worker \
        t3 worker drain --timeout "${TEATREE_DRAIN_TIMEOUT:-1800}" \
        || echo "deploy: drain window exceeded — proceeding (a stuck task re-queues via its lease lapse)"
fi

# Surface the WHY on a build/up failure — `set -e` would otherwise exit before
# the Action log sees anything but "exited (1)".
docker compose -f "$COMPOSE_FILE" up -d --build || {
    docker compose -f "$COMPOSE_FILE" ps
    docker compose -f "$COMPOSE_FILE" logs --tail 200 teatree-init teatree-worker teatree-admin
    exit 1
} >&2
# The swap completed: the fresh worker's init clears worker_quiescing, so the
# stranded-gate fail-safe above becomes a no-op from here on.
_SWAP_DONE=true

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
docker compose -f "$COMPOSE_FILE" ps >&2 || true
docker compose -f "$COMPOSE_FILE" logs --tail 50 teatree-init teatree-worker teatree-admin >&2 || true
exit 1
