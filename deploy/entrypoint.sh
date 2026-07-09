#!/usr/bin/env bash
# teatree headless deployment entrypoint. One image, three roles selected by
# $TEATREE_ROLE:
#   init   — one-shot prep (clone + editable install + t3 setup + DB config),
#            exits 0. worker/admin depend on its successful completion, so the
#            editable-install-on-the-shared-clone happens exactly once.
#   worker — runs `t3 worker` (the loop cadence owner), DEBUG off.
#   admin  — runs `t3 admin` (Django admin dev server) on the box loopback.
set -euo pipefail

ROLE="${TEATREE_ROLE:?TEATREE_ROLE must be one of: init, worker, admin}"
CLONE_DIR="${TEATREE_CLONE_DIR:-/home/teatree/teatree}"
REPO_URL="${TEATREE_REPO_URL:-https://github.com/souliane/teatree.git}"

# The loop and gh use GH_TOKEN from the ambient env for GitHub access, so the
# token never appears in a clone URL, argv, or logs.
if [ -n "${TEATREE_GH_TOKEN:-}" ]; then
    export GH_TOKEN="$TEATREE_GH_TOKEN"
fi

# Global git identity fallback — commits and the runtime loop need one.
git config --global user.name "${GIT_AUTHOR_NAME:-teatree}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-teatree@localhost}"
git config --global init.defaultBranch main
git config --global --add safe.directory "$CLONE_DIR"

ensure_clone() {
    if [ -e "$CLONE_DIR/.git" ]; then
        return 0
    fi
    if [ -n "${GH_TOKEN:-}" ]; then
        gh auth setup-git
    fi
    git clone "$REPO_URL" "$CLONE_DIR"
}

case "$ROLE" in
init)
    ensure_clone
    uv python install 3.13
    uv tool install --editable "$CLONE_DIR" --reinstall --python 3.13
    t3 setup
    t3 teatree db migrate
    # Values are JSON: enum strings are quoted, booleans and ints are bare.
    t3 teatree config_setting set agent_harness '"claude_sdk"'
    t3 teatree config_setting set agent_runtime '"headless"'
    t3 teatree config_setting set loop_runner_enabled true
    t3 teatree config_setting set provision_max_concurrency 1
    t3 teatree config_setting set provision_ram_ceiling_percent 75
    t3 teatree config_setting set max_concurrent_local_stacks 1
    echo "teatree-init: complete"
    ;;
worker)
    exec t3 worker
    ;;
admin)
    exec t3 admin --host 0.0.0.0 --port 8000 --no-browser
    ;;
*)
    echo "entrypoint: unknown TEATREE_ROLE '$ROLE' (expected init|worker|admin)" >&2
    exit 64
    ;;
esac
