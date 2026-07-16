#!/usr/bin/env bash
# teatree headless deployment entrypoint. One image, four roles selected by
# $TEATREE_ROLE:
#   init           — one-shot prep (clone + editable install + t3 setup + DB config),
#                    exits 0. worker/admin/slack-listener depend on its successful
#                    completion, so the editable-install-on-the-shared-clone happens once.
#   worker         — runs `t3 worker` (the loop cadence owner), DEBUG off.
#   admin          — runs `t3 admin` (Django admin under gunicorn, DEBUG off) on the box loopback.
#   slack-listener — runs `t3 slack listen` (the Socket-Mode receiver feeding the
#                    worker's drain-queue slot). Only meaningful when an overlay is
#                    Slack-enabled; a no-op-and-exit when none are.
set -euo pipefail

ROLE="${TEATREE_ROLE:?TEATREE_ROLE must be one of: init, worker, admin, slack-listener}"
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

# The GPG home is a dedicated bind mount of the host ~/.gnupg (see Dockerfile ENV
# + docker-compose credential plane); gpg refuses a home directory readable by
# group/other. Fix the mode before anything reads a credential — but only when
# the mount is writable (a hardened read-only mount would EROFS here under -e).
if [ -n "${GNUPGHOME:-}" ] && [ -d "$GNUPGHOME" ] && [ -w "$GNUPGHOME" ]; then
    chmod 700 "$GNUPGHOME"
fi

# True when the box pass store holds at least one Anthropic account entry —
# the option-b credential source (anthropic_oauth_pass_paths routing).
pass_store_has_anthropic() {
    pass ls anthropic >/dev/null 2>&1
}

# True when an anthropic/ entry actually DECRYPTS — `pass ls` only proves the
# .gpg files exist, not that gpg can read them (the private key may be absent or
# gpg-agent unable to start). Exit-code only; the plaintext never leaves gpg.
anthropic_credential_decrypts() {
    local store="${PASSWORD_STORE_DIR:-$HOME/.password-store}" entry
    entry="$(find "$store/anthropic" -type f -name '*.gpg' 2>/dev/null | head -1)"
    [ -n "$entry" ] || return 1
    entry="${entry#"$store/"}"
    pass show "${entry%.gpg}" >/dev/null 2>&1
}

# Fail loud, early, and actionably when a required runtime token is missing or
# does not authenticate — otherwise a green deploy hides a dead loop.
init_preflight() {
    if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        if ! pass_store_has_anthropic; then
            echo "entrypoint: no Anthropic credential - no CLAUDE_CODE_OAUTH_TOKEN and the pass store has no anthropic/ entries. Is host ~/.password-store bind-mounted and provisioned (anthropic/<account>/oauth-token)? See deploy/README.md - then re-run Deploy" >&2
            exit 1
        fi
        if ! anthropic_credential_decrypts; then
            echo "entrypoint: the pass store lists anthropic/ entries but gpg cannot DECRYPT them - the GPG private key is missing from $GNUPGHOME or gpg-agent cannot start (is host ~/.gnupg bind-mounted with the decryption key?) - then re-run Deploy" >&2
            exit 1
        fi
    fi
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
#
# `t3 loop disable` exits 0 even on an unregistered name (it only flips a real
# Loop row), so a typo would silently disable nothing and leave the real loop
# running — a double-run against the laptop. Validate the WHOLE list against the
# registered mini-loops first, so a bad value fails before anything is disabled.
disable_fleet_scoped_loops() {
    local raw="${TEATREE_DISABLED_LOOPS-inbox,review,directive_loop}"
    local field loop registered
    local fields=() requested=()
    IFS=',' read -ra fields <<<"$raw"
    for field in ${fields[@]+"${fields[@]}"}; do
        field="${field//[[:space:]]/}"
        [ -n "$field" ] && requested+=("$field")
    done
    [ ${#requested[@]} -gt 0 ] || return 0

    if ! registered="$(t3 loop list --json | jq -r '.mini_loops[].name')" || [ -z "$registered" ]; then
        echo "entrypoint: could not read the registered loops ('t3 loop list --json' failed or was empty) - confirm 't3 teatree db migrate' seeded the loops above and re-run Deploy" >&2
        exit 1
    fi

    for loop in "${requested[@]}"; do
        if ! grep -qxF "$loop" <<<"$registered"; then
            echo "entrypoint: TEATREE_DISABLED_LOOPS names an unknown loop '${loop}' - valid loops are: $(tr '\n' ' ' <<<"$registered")- fix the value in teatree.env and re-run Deploy" >&2
            exit 1
        fi
    done

    for loop in "${requested[@]}"; do
        if ! t3 loop disable "$loop"; then
            echo "entrypoint: 't3 loop disable ${loop}' FAILED - the DB-backed loop control plane is unreachable; confirm 't3 teatree db migrate' succeeded above and re-run Deploy" >&2
            exit 1
        fi
    done
}

ensure_clone() {
    if [ -e "$CLONE_DIR/.git" ]; then
        # The clone lives in a shared volume that outlives the image, so a
        # redeploy must bring it current or the stack keeps serving the code
        # from the first boot. SELF-HEAL: a stray feature branch checked out on
        # the runtime clone (or one whose upstream was deleted after its PR
        # merged) must never brick the H24 deploy — recover to the default
        # branch automatically; only a genuinely diverged default branch (local
        # commits that cannot fast-forward) still fails loud.
        git -C "$CLONE_DIR" fetch --prune origin
        local default_branch current
        default_branch="$(git -C "$CLONE_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')"
        default_branch="${default_branch:-main}"
        current="$(git -C "$CLONE_DIR" symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
        if [ "$current" != "$default_branch" ]; then
            echo "entrypoint: runtime clone was on '$current' (not '$default_branch') - self-healing to the default branch (any stray work stays on its branch)" >&2
            git -C "$CLONE_DIR" checkout --force "$default_branch"
        fi
        git -C "$CLONE_DIR" merge --ff-only "origin/$default_branch" || {
            echo "entrypoint: $CLONE_DIR default branch '$default_branch' has diverged (local commits that cannot fast-forward) - reconcile it on the box and re-run Deploy" >&2
            exit 1
        }
        return 0
    fi
    git clone "$REPO_URL" "$CLONE_DIR"
}

case "$ROLE" in
init)
    init_preflight
    ensure_clone
    uv python install 3.13
    # The [slack] extra pulls slack_sdk so the slack-listener role's Socket-Mode
    # receiver can open its WebSocket. Without it `t3 slack listen` degrades to a
    # no-op ("slack_sdk not installed") and inbound Slack never reaches the loop.
    uv tool install --editable "$CLONE_DIR[slack]" --reinstall --python 3.13
    t3 setup
    t3 teatree db migrate
    # Values are JSON: enum strings are quoted, booleans and ints are bare.
    seed_setting agent_harness '"claude_sdk"'
    seed_setting agent_runtime '"headless"'
    seed_setting loop_runner_enabled true
    seed_setting provision_max_concurrency 1
    seed_setting provision_ram_ceiling_percent 75
    seed_setting max_concurrent_local_stacks 1
    # The admin binds the box loopback (host networking), so auto-login fires for
    # the SSH-tunnelled 127.0.0.1 request — no admin password behind the tunnel.
    seed_setting admin_autologin_enabled true
    disable_fleet_scoped_loops
    echo "teatree-init: complete"
    ;;
worker)
    exec t3 worker
    ;;
slack-listener)
    # Socket-Mode receiver: one WebSocket per slack-enabled overlay, writing
    # inbound events to the JSONL queue that the worker's drain-queue slot
    # drains, acks with 👀, and dispatches. `t3 slack listen` exits non-zero
    # when no overlay is Slack-enabled; `restart: unless-stopped` then simply
    # keeps a harmless retry loop on a box that has no Slack overlay yet.
    exec t3 slack listen
    ;;
admin)
    # Bind the box loopback (the service uses host networking) so the SSH-tunnel
    # request arrives as 127.0.0.1 and clears the middleware's loopback check.
    exec t3 admin --host 127.0.0.1 --port 8000 --no-browser
    ;;
*)
    echo "entrypoint: unknown TEATREE_ROLE '$ROLE' (expected init|worker|admin|slack-listener)" >&2
    exit 64
    ;;
esac
