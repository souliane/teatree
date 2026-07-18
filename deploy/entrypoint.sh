#!/usr/bin/env bash
# teatree headless deployment entrypoint. One image, five roles selected by
# $TEATREE_ROLE:
#   init           — one-shot prep (clone + editable install + t3 setup + DB config),
#                    exits 0. worker/admin/slack-listener depend on its successful
#                    completion, so the editable-install-on-the-shared-clone happens once.
#   worker         — runs `t3 worker` (the loop cadence owner), DEBUG off.
#   admin          — runs `t3 admin` (Django admin under gunicorn, DEBUG off) on the box loopback.
#   slack-listener — runs `t3 slack listen` (the Socket-Mode receiver feeding the
#                    worker's drain-queue slot). Only meaningful when an overlay is
#                    Slack-enabled; a no-op-and-exit when none are.
#   watchdog       — runs `deploy/watchdog.sh --loop` (the in-daemon self-heal
#                    sidecar). Dispatched BEFORE the common preamble below: it has
#                    no env_file/GH token/gnupg mount and runs as root, so the
#                    gh-auth / git-config / chmod-GNUPGHOME preamble is noise or a
#                    crash for it.
set -euo pipefail

ROLE="${TEATREE_ROLE:?TEATREE_ROLE must be one of: init, worker, admin, slack-listener, watchdog}"

# Dispatch the watchdog role FIRST — before the credential/git preamble that the
# other roles need but the watchdog neither has nor wants (root, no secrets).
if [ "$ROLE" = watchdog ]; then
    exec bash /home/teatree/teatree-deploy/deploy/watchdog.sh --loop
fi

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

# Fleet role split: this instance must run its own loops and NOT the loops another
# fleet member owns. The box HOSTS the DM-only Slack conversational loop for the
# owner overlay, so `inbox` — the inbound-messaging scanners (Slack DM →
# PendingChatInjection, review-intent, red-card, mentions) — MUST run here; it
# feeds the drain → 👀-ack → answer cycle that posts replies. The COLLEAGUE-facing
# Slack loops the laptop owns stay off here: `review` (colleague PR review → Slack)
# and `directive_loop` (asks the human via Slack).
#
# Per-loop enable/disable/pause/resume is now EMERGENCY-only (#3248): the normal
# handle is presets/schedules and the emergency per-loop handle is `t3 loop
# override`. Neither presets, schedules, nor `t3 loop override` can express this
# box's per-loop role, and — critically — none of them can lift a durable
# `LoopState` HOLD: admission resolves hold > forced > preset > base, so a loop a
# prior deploy left in a DISABLED hold (older images ran `t3 loop disable inbox`)
# stays dead under any preset/schedule/override. Clearing a hold has exactly ONE
# handle: `t3 loop enable`, which is emergency-gated. So this box declares its role
# on the two authoritative planes that actually beat everything below them:
#
#   * ENABLED set (default `inbox`) → `t3 loop enable <name> --emergency`, which
#     clears any stale hold AND sets `Loop.enabled=True`, so a box whose inbox a
#     prior deploy durably disabled recovers. Idempotent (a no-op when already on).
#   * DISABLED set (default `review,directive_loop`) → `t3 loop override <name> off`,
#     the sanctioned, NON-emergency forced-off that supersedes the deprecated
#     `t3 loop disable`. Forced-off beats the preset mask AND the base config, so a
#     colleague/human-facing loop stays off here regardless of any mode the owner
#     later selects. Idempotent.
#
# TEATREE_ENABLED_LOOPS / TEATREE_DISABLED_LOOPS (comma-separated, from teatree.env)
# override the defaults; empty values act on nothing. Every name in BOTH lists is
# validated against the registered mini-loops first, so a typo fails the deploy
# loudly before anything is touched (rather than silently mis-configuring the box).
apply_fleet_loop_policy() {
    local enabled_raw="${TEATREE_ENABLED_LOOPS-inbox}"
    local disabled_raw="${TEATREE_DISABLED_LOOPS-review,directive_loop}"
    local field loop registered
    local fields=() enable_loops=() disable_loops=()

    IFS=',' read -ra fields <<<"$enabled_raw"
    for field in ${fields[@]+"${fields[@]}"}; do
        field="${field//[[:space:]]/}"
        [ -n "$field" ] && enable_loops+=("$field")
    done
    fields=()
    IFS=',' read -ra fields <<<"$disabled_raw"
    for field in ${fields[@]+"${fields[@]}"}; do
        field="${field//[[:space:]]/}"
        [ -n "$field" ] && disable_loops+=("$field")
    done
    [ $((${#enable_loops[@]} + ${#disable_loops[@]})) -gt 0 ] || return 0

    if ! registered="$(t3 loop list --json | jq -r '.mini_loops[].name')" || [ -z "$registered" ]; then
        echo "entrypoint: could not read the registered loops ('t3 loop list --json' failed or was empty) - confirm 't3 teatree db migrate' seeded the loops above and re-run Deploy" >&2
        exit 1
    fi

    for loop in ${enable_loops[@]+"${enable_loops[@]}"} ${disable_loops[@]+"${disable_loops[@]}"}; do
        if ! grep -qxF "$loop" <<<"$registered"; then
            echo "entrypoint: TEATREE_ENABLED_LOOPS/TEATREE_DISABLED_LOOPS names an unknown loop '${loop}' - valid loops are: $(tr '\n' ' ' <<<"$registered")- fix the value in teatree.env and re-run Deploy" >&2
            exit 1
        fi
    done

    # A loop in BOTH lists is a contradiction: the ENABLE pass forces it on, then
    # the DISABLE pass would immediately force it off (admission resolves
    # forced > preset > base), leaving a sanctioned-enabled loop silently MASKED
    # on every init. This is exactly how `inbox` regressed (teatree.env carried it
    # in both lists). ENABLED wins (it is the stronger, emergency-gated signal and
    # the operator's explicit "must run here"): drop such loops from the disable
    # set and WARN loudly. Resolving rather than `exit 1` is deliberate — a hard
    # failure here would crash-loop init on an already-deployed box that carries
    # the overlap (the very config that shipped), turning a silent mask into an
    # outage. The warning tells the operator to de-dup teatree.env.
    local pruned_disable=()
    for loop in ${disable_loops[@]+"${disable_loops[@]}"}; do
        local overlaps=
        for other in ${enable_loops[@]+"${enable_loops[@]}"}; do
            if [ "$loop" = "$other" ]; then
                overlaps=1
                break
            fi
        done
        if [ -n "$overlaps" ]; then
            echo "entrypoint: loop '${loop}' is in BOTH TEATREE_ENABLED_LOOPS and TEATREE_DISABLED_LOOPS - keeping it ENABLED (would otherwise be re-masked every restart); remove it from TEATREE_DISABLED_LOOPS in teatree.env to silence this warning" >&2
        else
            pruned_disable+=("$loop")
        fi
    done
    disable_loops=(${pruned_disable[@]+"${pruned_disable[@]}"})

    # ENABLE clears any durable hold (only `enable` can) and sets Loop.enabled=True.
    # It does NOT lift a stale forced-OFF override — so a loop this box left in the
    # DISABLED set on a PRIOR deploy stays masked even after being promoted to the
    # ENABLED set here (the override outlives the config change in LoopState). Clear
    # the override right after enabling so a sanctioned-enabled loop can never remain
    # forced off by leftover state; `clear` is neutral, so a still-enabled loop keeps
    # running via Loop.enabled=True.
    for loop in ${enable_loops[@]+"${enable_loops[@]}"}; do
        if ! t3 loop enable "$loop" --emergency; then
            echo "entrypoint: 't3 loop enable ${loop} --emergency' FAILED - the DB-backed loop control plane is unreachable; confirm 't3 teatree db migrate' succeeded above and re-run Deploy" >&2
            exit 1
        fi
        if ! t3 loop override "$loop" clear --reason "fleet policy: ${loop} is sanctioned-enabled here; drop any stale forced-off override from a prior deploy"; then
            echo "entrypoint: 't3 loop override ${loop} clear' FAILED - the DB-backed loop control plane is unreachable; confirm 't3 teatree db migrate' succeeded above and re-run Deploy" >&2
            exit 1
        fi
    done

    # DISABLE via the forced-off override plane (beats preset + base config), the
    # sanctioned non-emergency successor to the now-refused `t3 loop disable`.
    for loop in ${disable_loops[@]+"${disable_loops[@]}"}; do
        if ! t3 loop override "$loop" off --reason "fleet policy (DM-only box): ${loop} must not run here"; then
            echo "entrypoint: 't3 loop override ${loop} off' FAILED - the DB-backed loop control plane is unreachable; confirm 't3 teatree db migrate' succeeded above and re-run Deploy" >&2
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
    # prek (the pre-commit reimplementation) is a DEV-group dependency, so the
    # editable tool install above does NOT provide it. Worktree provisioning
    # (`prek_hook.install`) and the base-clone commit/push gates need `prek` on
    # PATH; install it as a standalone uv tool (pinned to the lockfile) into the
    # shared teatree_uv volume so every role sees it. Runtime (not Dockerfile):
    # /opt/teatree/uv is a named volume that shadows any image-baked install.
    uv tool install prek==0.3.13
    # Install the commit/push gate hooks on the base clone's SHARED hooks dir
    # (git links every worktree to it), so the privacy leak gate (#685), the
    # foreign-MR guard, banned-terms, and the push gates actually fire on the
    # loop's pushes. Without this the migrated box had an EMPTY .git/hooks and
    # every gate was silently bypassed. Idempotent; harden the baked PREK path
    # to a PATH lookup (souliane/teatree#1462) so a torn-down worktree can't
    # leave a stale absolute path in the shared hook.
    ( cd "$CLONE_DIR" && prek install -f \
        && sed -i 's#^PREK="/opt/teatree/uv/tools/prek/bin/prek"#PREK="prek"#' \
            .git/hooks/pre-push .git/hooks/pre-commit .git/hooks/commit-msg 2>/dev/null )
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
    apply_fleet_loop_policy
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
    #
    # Drain + 👀-ack captured DMs on a cadence: the reactive loop-drain-queue
    # slot is not bootstrapped under `t3 worker` in headless, so the listener's
    # captures would never reach an observable state without this. `t3 slack
    # check` drains the JSONL queue and, unlike the drain-queue loop, is NOT
    # gated by the worker singleton. Failure-tolerant (`|| true`) and
    # backgrounded so a non-zero check never trips `set -e` or crashes the
    # foreground listener.
    SLACK_CHECK_INTERVAL_SECONDS=15
    ( while true; do t3 slack check >/dev/null 2>&1 || true; sleep "$SLACK_CHECK_INTERVAL_SECONDS"; done ) &
    exec t3 slack listen
    ;;
admin)
    # Bind the box loopback (the service uses host networking) so the SSH-tunnel
    # request arrives as 127.0.0.1 and clears the middleware's loopback check.
    exec t3 admin --host 127.0.0.1 --port 8000 --no-browser
    ;;
*)
    echo "entrypoint: unknown TEATREE_ROLE '$ROLE' (expected init|worker|admin|slack-listener|watchdog)" >&2
    exit 64
    ;;
esac
