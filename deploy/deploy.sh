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

# Docker installed + enabled on boot (so the stack autostarts after a reboot,
# alongside the compose restart policies). is-active needs no root, so the
# common case (docker already running) never invokes sudo.
if ! command -v docker >/dev/null 2>&1; then
    echo "deploy: docker is not installed — see deploy/README.md bootstrap." >&2
    exit 1
fi
if ! systemctl is-active --quiet docker; then
    sudo systemctl enable --now docker
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "deploy: missing $ENV_FILE (the deploy workflow writes it before this runs)." >&2
    exit 1
fi

# Bring the build context current (fast-forward only — never clobber local work).
git -C "$REPO_ROOT" fetch --prune origin
git -C "$REPO_ROOT" pull --ff-only

docker compose -f "$COMPOSE_FILE" up -d --build

# Wait for the admin dev server on the box loopback (init clone + install can
# take a few minutes on first run).
echo "deploy: waiting for the admin service on 127.0.0.1:8000 ..."
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "http://127.0.0.1:8000/admin/login/"; then
        echo "deploy: admin is up; stack converged."
        exit 0
    fi
    sleep 10
done

echo "deploy: admin did not become ready in time — recent logs:" >&2
docker compose -f "$COMPOSE_FILE" ps >&2 || true
docker compose -f "$COMPOSE_FILE" logs --tail 50 teatree-init teatree-admin >&2 || true
exit 1
