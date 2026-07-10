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

# Configure git to use gh as the https credential helper for EVERY role (idempotent):
# the worker/admin `git push` over https needs it too, not just the init clone.
if [ -n "${GH_TOKEN:-}" ]; then
    gh auth setup-git
fi

# Global git identity fallback — commits and the runtime loop need one.
git config --global user.name "${GIT_AUTHOR_NAME:-teatree}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-teatree@localhost}"
git config --global init.defaultBranch main
git config --global --add safe.directory "$CLONE_DIR"

# Fail loud, early, and actionably when a required runtime token is missing or
# does not authenticate — otherwise a green deploy hides a dead loop.
init_preflight() {
    : "${CLAUDE_CODE_OAUTH_TOKEN:?MISSING CLAUDE_CODE_OAUTH_TOKEN - set the repo secret and re-run Deploy}"
    : "${TEATREE_GH_TOKEN:?MISSING TEATREE_GH_TOKEN - set the repo secret and re-run Deploy}"
    : "${GIT_AUTHOR_NAME:?MISSING GIT_AUTHOR_NAME - set the repo secret and re-run Deploy}"
    : "${GIT_AUTHOR_EMAIL:?MISSING GIT_AUTHOR_EMAIL - set the repo secret and re-run Deploy}"
    if ! gh auth status >/dev/null 2>&1; then
        echo "entrypoint: TEATREE_GH_TOKEN does not authenticate with GitHub - rotate the token and re-run Deploy" >&2
        exit 1
    fi
}

# Seed a config value only when the operator has NOT already overridden it, so a
# re-deploy never clobbers an operator's change (e.g. loop_runner_enabled=false).
seed_setting() {
    if t3 teatree config_setting get "$1" 2>/dev/null | grep -q 'source: db'; then
        echo "teatree-init: $1 already set (operator override preserved) - skipping"
    else
        t3 teatree config_setting set "$1" "$2"
    fi
}

# Fleet role split: this instance must not run the loops another fleet member
# owns. The box provisions no Slack credential (see README), so the Slack-facing
# loops — broken here AND duplicates of the laptop's — are disabled through the
# one DB-backed per-loop control plane. `t3 loop disable` is idempotent, so a
# re-deploy converges. TEATREE_DISABLED_LOOPS (comma-separated, from teatree.env)
# overrides the default; an empty value runs every loop here.
disable_fleet_scoped_loops() {
    local raw="${TEATREE_DISABLED_LOOPS-inbox,review,directive_loop}"
    local loops loop
    IFS=',' read -ra loops <<<"$raw"
    for loop in ${loops[@]+"${loops[@]}"}; do
        loop="${loop//[[:space:]]/}"
        [ -n "$loop" ] || continue
        if ! t3 loop disable "$loop"; then
            echo "entrypoint: 't3 loop disable ${loop}' FAILED - the DB-backed loop control plane is unreachable; confirm 't3 teatree db migrate' succeeded above and re-run Deploy" >&2
            exit 1
        fi
    done
}

ensure_clone() {
    if [ -e "$CLONE_DIR/.git" ]; then
        return 0
    fi
    git clone "$REPO_URL" "$CLONE_DIR"
}

case "$ROLE" in
init)
    init_preflight
    ensure_clone
    uv python install 3.13
    uv tool install --editable "$CLONE_DIR" --reinstall --python 3.13
    t3 setup
    t3 teatree db migrate
    # Values are JSON: enum strings are quoted, booleans and ints are bare.
    seed_setting agent_harness '"claude_sdk"'
    seed_setting agent_runtime '"headless"'
    seed_setting loop_runner_enabled true
    seed_setting provision_max_concurrency 1
    seed_setting provision_ram_ceiling_percent 75
    seed_setting max_concurrent_local_stacks 1
    disable_fleet_scoped_loops
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
