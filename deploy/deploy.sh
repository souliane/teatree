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

# The credential-plane bind sources must pre-exist owned by the deploy user —
# dockerd would otherwise create a missing source ROOT-owned, locking the user
# out of later `pass insert` provisioning. Empty dirs are the sane degradation
# for an env-token box (init's preflight then falls through to CLAUDE_CODE_OAUTH_TOKEN).
install -d -m 700 "$HOME/.password-store" "$HOME/.gnupg"

# Surface the WHY on a build/up failure — `set -e` would otherwise exit before
# the Action log sees anything but "exited (1)".
docker compose -f "$COMPOSE_FILE" up -d --build || {
    docker compose -f "$COMPOSE_FILE" ps
    docker compose -f "$COMPOSE_FILE" logs --tail 200 teatree-init teatree-worker teatree-admin
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
docker compose -f "$COMPOSE_FILE" ps >&2 || true
docker compose -f "$COMPOSE_FILE" logs --tail 50 teatree-init teatree-worker teatree-admin >&2 || true
exit 1
