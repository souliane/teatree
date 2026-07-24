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

# gpg refuses a group/other-readable home, so normalise GNUPGHOME's mode BEFORE
# the boot-time `pass show` reads below can decrypt — only when the mount is
# writable (a hardened read-only mount would EROFS here under -e).
if [ -n "${GNUPGHOME:-}" ] && [ -d "$GNUPGHOME" ] && [ -w "$GNUPGHOME" ]; then
    chmod 700 "$GNUPGHOME"
fi

# Route ALL runtime temp to DISK, never the box's small RAM-backed tmpfs. The
# host /tmp is a ~16G tmpfs; the spawned headless `claude` sessions, `pytest`, and
# `uv` write scratch there and can fill it to 100% (ENOSPC), wedging the whole box.
# The container root is a large overlay DISK, so ``/var/tmp`` (always present,
# world-writable+sticky, disk-backed on both host and container) is a safe temp
# root that never touches the RAM tmpfs. Exported for EVERY non-watchdog role
# BEFORE the role `exec`s, so the role process and its children — the headless
# `claude` subprocess (which inherits every non-``GIT_*`` var, see
# teatree.utils.git_run.git_env_without_overrides), pytest, and uv — all land their
# scratch on disk. The container settings.json seed (from the image-baked template)
# also carries ``TMPDIR``/``PYTEST_DEBUG_TEMPROOT`` so an agent's Bash tool inherits
# it too; this export additionally covers the role process itself. Overridable via
# ``TEATREE_DISK_TMPDIR`` for a box whose disk temp lives elsewhere.
setup_disk_tmpdir() {
    local tmproot="${TEATREE_DISK_TMPDIR:-/var/tmp}"
    mkdir -p "$tmproot"
    export TMPDIR="$tmproot"
    export PYTEST_DEBUG_TEMPROOT="$tmproot"
}
setup_disk_tmpdir

# Source a runtime secret from the box pass store when its env var is unset,
# keeping the plaintext out of teatree.env and off argv/logs (#3454). An env
# value always wins (eval/CI paths and a deliberate literal override); the pass
# store is the fallback that lets a rotated secret be picked up at boot without
# rewriting teatree.env. `pass show` writes only to the captured stdout here.
source_secret_from_pass() {
    local var="$1" path="$2" value
    [ -n "${!var:-}" ] && return 0
    value="$(pass show "$path" 2>/dev/null | head -n1)" || return 0
    if [ -n "$value" ]; then
        export "$var"="$value"
    fi
    return 0
}

# GitHub token + admin password default to the box's provisioned pass paths;
# override either in teatree.env when the store is laid out differently.
source_secret_from_pass TEATREE_GH_TOKEN "${TEATREE_GH_TOKEN_PASS_PATH:-github/souliane/pat}"
source_secret_from_pass T3_ADMIN_PASSWORD "${T3_ADMIN_PASSWORD_PASS_PATH:-teatree/admin-password}"

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

# Parse ``owner/repo`` from the https/ssh clone URL (empty when unparsable).
gh_repo_slug() {
    local url="${TEATREE_REPO_URL:-$REPO_URL}"
    url="${url#https://github.com/}"
    url="${url#ssh://git@github.com/}"
    url="${url#git@github.com:}"
    url="${url%.git}"
    local owner="${url%%/*}" rest="${url#*/}" repo
    repo="${rest%%/*}"
    if [ -n "$owner" ] && [ -n "$repo" ] && [ "$owner" != "$url" ]; then
        printf '%s/%s' "$owner" "$repo"
    fi
}

# True (0) on a genuine token-DENIAL signal (vs a transient fault) — mirrors the Python gate's _DENIED_SIGNALS.
_gh_metadata_denied() {
    grep -qiE 'not accessible|not found|bad credentials|requires authentication|must be authenticated' <<<"$1"
}

# True (0) when a side-effect-free probe is DENIED — one check covers write and read probes alike (see gh_token_preflight's module docstring).
_gh_probe_denied() {
    local out
    out="$(gh api "$@" 2>&1 || true)"
    grep -qi "not accessible" <<<"$out"
}

# Extract `default_branch` from the `-i` metadata read's body — mirrors gh_token_preflight._parse_default_branch.
_gh_default_branch() {
    local body
    body="$(sed -n '/^\r\{0,1\}$/,$p' <<<"$1" | tail -n +2)"
    jq -r '.default_branch // empty' <<<"$body" 2>/dev/null
}

# GitHub has no API to widen a token's grant — mirrors gh_token_preflight's URL constants.
_GH_CLASSIC_TOKEN_URL="https://github.com/settings/tokens/new?scopes=repo,workflow,read:project&description=teatree"
_GH_FINE_GRAINED_TOKENS_URL="https://github.com/settings/personal-access-tokens"

# Mirrors gh_token_preflight's verdict semantics (#3405/#3436/#3477, pinned by a test): a REQUIRED denial exits 1 (never-lockout: only these four), a RECOMMENDED gap only WARNs, a transient failure retries then WARNs.
assert_gh_token_permissions() {
    local slug meta rc scopes attempt missing=() warn_missing=() default_branch scope_body
    local backoff="${TEATREE_GH_PREFLIGHT_BACKOFF_SECONDS:-2}"
    slug="$(gh_repo_slug)"
    if [ -z "$slug" ]; then
        echo "entrypoint: could not resolve the GitHub repo slug from '${TEATREE_REPO_URL:-$REPO_URL}' - skipping token-permission preflight" >&2
        return 0
    fi

    # Metadata read with -i so the X-OAuth-Scopes header comes back; retry a transient failure.
    rc=0
    for attempt in 1 2 3; do
        meta="$(gh api -i "repos/$slug" 2>&1)" && rc=0 && break || rc=$?
        if _gh_metadata_denied "$meta"; then
            echo "entrypoint: TEATREE_GH_TOKEN cannot read repos/$slug (metadata: read) - the token has no access to the repo. Grant it and re-run Deploy" >&2
            exit 1
        fi
        echo "entrypoint: gh token preflight: transient failure reading repos/$slug (attempt $attempt/3, rc=$rc) - retrying" >&2
        if [ "$attempt" -lt 3 ]; then
            sleep "$((attempt * backoff))"
        fi
    done
    if [ "$rc" -ne 0 ]; then
        echo "entrypoint: gh token preflight: repos/$slug still unreachable after retries (indeterminate, rc=$rc) - SKIPPING the write-permission preflight (a transient GitHub/network fault, not a denial); the loop surfaces any real gap on its first write" >&2
        return 0
    fi

    default_branch="$(_gh_default_branch "$meta")"

    # Classic PAT? The per-route probe fails OPEN for it — judge by exact scope-token membership instead.
    if scopes="$(grep -i '^x-oauth-scopes:' <<<"$meta")"; then
        scope_body="${scopes#*:}"
        if ! grep -qE '(^|[[:space:],])repo([[:space:],]|$)' <<<"$scope_body"; then
            echo "entrypoint: TEATREE_GH_TOKEN is a classic PAT WITHOUT the 'repo' scope - the loop's 'gh issue'/'gh pr'/push writes will fail mid-run with 'Resource not accessible by personal access token'. Grant the 'repo' scope on the token and re-run Deploy" >&2
            exit 1
        fi
        grep -qE '(^|[[:space:],])workflow([[:space:],]|$)' <<<"$scope_body" || warn_missing+=("workflows: write")
        grep -qE '(^|[[:space:],])read:project([[:space:],]|$)' <<<"$scope_body" || warn_missing+=("projects: read")
        if [ ${#warn_missing[@]} -gt 0 ]; then
            echo "entrypoint: WARN TEATREE_GH_TOKEN (classic PAT) is missing recommended permission(s): ${warn_missing[*]} - workflows:write gates pushing PRs that touch .github/workflows/*, projects:read gates GitHub Projects board sync; neither blocks boot. Classic tokens cannot be widened via the API - create a new one: $_GH_CLASSIC_TOKEN_URL" >&2
        fi
        echo "teatree-init: GitHub token permissions verified (classic PAT with 'repo' scope on $slug)"
        return 0
    fi

    # Fine-grained PAT: REQUIRED per-permission route probes (403 = missing, 404 = present).
    _gh_probe_denied --method PATCH "repos/$slug/issues/0" -f state=open && missing+=("issues: write")
    _gh_probe_denied --method PATCH "repos/$slug/pulls/0" -f state=open && missing+=("pull_requests: write")
    _gh_probe_denied --method PATCH "repos/$slug/git/refs/heads/teatree-preflight-nonexistent" && missing+=("contents: write")
    if [ ${#missing[@]} -gt 0 ]; then
        echo "entrypoint: TEATREE_GH_TOKEN is missing GitHub permission(s): ${missing[*]} - the loop's 'gh issue'/'gh pr'/push writes will fail mid-run with 'Resource not accessible by personal access token'. Grant them on the token and re-run Deploy" >&2
        exit 1
    fi

    # RECOMMENDED (WARN-tier) probes — never exit 1. workflows:write is never actively probed (see above).
    warn_missing=("workflows: write")
    _gh_probe_denied --method POST "repos/$slug/actions/workflows/0/dispatches" -f ref=teatree-preflight-nonexistent &&
        warn_missing+=("actions: write")
    _gh_probe_denied "repos/$slug/actions/artifacts?per_page=1" && warn_missing+=("actions: read")
    # `gh secret set` / `gh variable set` PUT these routes; DELETE hits the same write gate,
    # so a sentinel name that never exists probes the grant with no side effect.
    _gh_probe_denied --method DELETE "repos/$slug/actions/secrets/TEATREE_PREFLIGHT_NONEXISTENT" &&
        warn_missing+=("secrets: write")
    _gh_probe_denied --method DELETE "repos/$slug/actions/variables/TEATREE_PREFLIGHT_NONEXISTENT" &&
        warn_missing+=("variables: write")
    if [ -n "$default_branch" ]; then
        _gh_probe_denied "repos/$slug/commits/$default_branch/check-runs?per_page=1" && warn_missing+=("checks: read")
        _gh_probe_denied "repos/$slug/commits/$default_branch/status" && warn_missing+=("statuses: read")
    fi
    # projects: read needs an overlay's configured Projects-v2 board, which this
    # bash preflight cannot see — `t3 doctor check` probes it when configured.
    if [ ${#warn_missing[@]} -gt 0 ]; then
        echo "entrypoint: WARN TEATREE_GH_TOKEN is missing recommended permission(s): ${warn_missing[*]} - these degrade optional features (CI trigger/status, auto-merge's required-checks rollup, workflow-file pushes, the CI OAuth-account switch) but do NOT block boot. Fine-grained tokens cannot be widened via the API either - recreate it with these permissions added: $_GH_FINE_GRAINED_TOKENS_URL" >&2
    fi
    echo "teatree-init: GitHub token permissions verified (required issues/pull_requests/contents write present on $slug)"
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
    # #3405: authentication is not authorization - verify the token can WRITE the
    # resources the loop mutates (issues/pull_requests/contents), failing loud now
    # rather than mid-run with 'Resource not accessible by personal access token'.
    assert_gh_token_permissions
}

# Provision ~/.claude/settings.json so the containerized (headless) agent is
# CONFIGURABLE — model, permission mode, autoMode grants, tool-use concurrency —
# instead of running on stock Claude Code defaults (#3359). Without this the
# claude_sdk harness spawns the `claude` CLI, which reads ~/.claude/settings.json,
# and that file simply never existed in the container.
#
# The reviewable default lives in the committed, image-baked
# deploy/claude-settings.template.json; three env vars override the box-specific knobs.
# Deploy-managed keys WIN over an existing file (a redeploy re-asserts the intended
# config) while UNMANAGED keys the later `t3 setup` adds — notably statusLine — are
# preserved (`jq '.[0] * .[1]'` deep-merges, right wins). A pre-existing INVALID
# settings.json is REPLACED with the managed config (the merge cannot parse it, and a
# corrupt file downstream bricks `t3 setup` / the `claude` CLI). MUST run before
# `t3 setup`.
seed_claude_settings() {
    local template="${TEATREE_CLAUDE_SETTINGS_TEMPLATE:-/usr/local/share/teatree/claude-settings.template.json}"
    local target="$HOME/.claude/settings.json"
    if [ ! -f "$template" ]; then
        echo "teatree-init: no claude-settings template at $template - skipping (agent runs on CLI defaults)" >&2
        return 0
    fi
    mkdir -p "$HOME/.claude"
    local managed
    # Apply the TEATREE_CLAUDE_* box-knob overrides via the ONE shared resolver in
    # cli/setup/claude_settings.py, so this seed and the host-side `t3 doctor` drift
    # check (managed_key_drift) resolve the SAME effective config (#3437). The module
    # is pure-stdlib, so `python3 <file>` runs it without importing the teatree CLI.
    local resolver="$CLONE_DIR/src/teatree/cli/setup/claude_settings.py"
    if ! managed="$(python3 "$resolver" "$template")"; then
        echo "teatree-init: failed to resolve claude-settings template - skipping" >&2
        return 0
    fi
    # Deep-merge over an EXISTING valid file (right wins) so unmanaged keys survive;
    # but a pre-existing INVALID settings.json cannot be parsed by the merge and, left
    # in place, bricks `t3 setup` / the `claude` CLI and silently drops the managed
    # config. Validate first and REPLACE a corrupt (or unmergeable) file with the
    # managed config rather than aborting init or leaving it broken.
    if [ -f "$target" ] && jq -e . "$target" >/dev/null 2>&1; then
        if jq -s '.[0] * .[1]' "$target" <(printf '%s' "$managed") >"$target.tmp" 2>/dev/null; then
            mv "$target.tmp" "$target"
        else
            rm -f "$target.tmp"
            echo "teatree-init: could not merge existing ~/.claude/settings.json - replacing it with the managed config" >&2
            printf '%s\n' "$managed" >"$target"
        fi
    else
        if [ -f "$target" ]; then
            echo "teatree-init: existing ~/.claude/settings.json is not valid JSON - replacing it with the managed config" >&2
        fi
        printf '%s\n' "$managed" >"$target"
    fi
    echo "teatree-init: provisioned ~/.claude/settings.json (model=$(jq -r .model "$target"), mode=$(jq -r .permissions.defaultMode "$target"))"
}

# Provision the per-container Claude runtime the spawned `claude` agent needs:
# ~/.claude/settings.json (seed_claude_settings) AND `t3 setup` (skill links, the
# t3@souliane plugin registration via PluginRegistrar.install, statusLine, MCP
# registration). This MUST run in EVERY agent-spawning role, not just init: the
# `~/.claude` dir is PER-CONTAINER ephemeral (docker-compose.yml bind-mounts only
# ~/.claude/projects — credentials stay host-only), so init's registration lands in
# the init container's throwaway ~/.claude and never reaches worker/admin/slack-
# listener. Without this, the worker's `claude` has no ~/.claude/plugins and no
# enabledPlugins, so factory agents load ZERO skills. `t3 setup` is idempotent and
# claude-env-focused, and these roles `depends_on` a completed init (shared clone +
# editable install on the teatree_uv volume are present), so it is safe per-role.
prepare_claude_runtime() {
    seed_claude_settings
    t3 setup
}

# VERIFY the agent's skills are actually available after `prepare_claude_runtime`:
# the ``t3@souliane`` plugin is registered in ~/.claude/plugins/installed_plugins.json
# with a resolvable install path AND enabled in ~/.claude/settings.json. Returns
# non-zero when any signal is missing — the exact "agents would run SKILL-LESS"
# condition. The worker treats this as a HARD startup precondition (owner directive:
# PREFER HARD FAIL over silently running with a critical capability missing).
verify_agent_skills() {
    local settings="$HOME/.claude/settings.json"
    local installed="$HOME/.claude/plugins/installed_plugins.json"
    jq -e '.enabledPlugins."t3@souliane" == true' "$settings" >/dev/null 2>&1 || return 1
    local install_path
    install_path="$(jq -r '(.plugins."t3@souliane" // [])[0].installPath // empty' "$installed" 2>/dev/null)" || return 1
    [ -n "$install_path" ] && [ -d "$install_path" ]
}

# Seed a config value through the provenance-aware DEPLOY seed (#3435). The ORM
# command NEVER writes a value equal to the code default (a code-default seed only
# FREEZES a future default change), PRESERVES any operator override, re-seeds a row
# this deploy still owns when the SHIPPED default changed, and records provenance
# so a later `t3 doctor --repair` clears only an entrypoint-seeded pin — never an
# operator's deliberate one. Idempotent across redeploys.
seed_setting() {
    # A single provisioning seed is NON-FATAL: one setting the runtime already
    # has a sane code default for must never brick the whole stack (init failing
    # takes worker/admin/slack-listener down with it, since they `depends_on` a
    # successful init). Warn to stderr and continue under `set -e`; the runtime
    # falls back to the code default and a later redeploy re-seeds it.
    if ! t3 teatree config_setting seed "$1" "$2"; then
        echo "teatree-init: WARNING seed of '$1' failed ('t3 teatree config_setting seed' exited non-zero); continuing — the runtime uses the code default for it. Fix and re-run Deploy to persist an override." >&2
    fi
}

# Fleet role split: this instance must run its own loops and NOT the loops another
# fleet member owns. The box HOSTS the DM-only Slack conversational loop for the
# owner overlay, so `inbox` — the inbound-messaging scanners (Slack DM →
# PendingChatInjection, review-intent, red-card, mentions) — MUST run here; it
# feeds the drain → 👀-ack → answer cycle that posts replies. The COLLEAGUE-facing
# Slack loop the laptop owns stays off here: `review` (colleague PR review → Slack).
#
# OWNER-INTAKE loops are NEVER forced off here (#3632): `directive_loop` interprets
# the owner's captured directives and `dispatch` posts deferred owner questions.
# `autonomous_away` means the human is unreachable *now* — captured intent must
# QUEUE for later, not be dropped unread. A prior default forced `directive_loop`
# off on every deploy, so captured owner directives sat uninterpreted for days; the
# owner-intake set (`t3 loop intake-loops`) is pruned from the DISABLED set below.
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
#   * DISABLED set (default `review`) → `t3 loop override <name> off`, the
#     sanctioned, NON-emergency forced-off that supersedes the deprecated
#     `t3 loop disable`. Forced-off beats the preset mask AND the base config, so a
#     colleague/human-facing loop stays off here regardless of any mode the owner
#     later selects. Idempotent. Owner-intake loops (`t3 loop intake-loops`) are
#     pruned from this set before it is applied, so they can never be re-masked.
#
# TEATREE_ENABLED_LOOPS / TEATREE_DISABLED_LOOPS (comma-separated, from teatree.env)
# override the defaults; empty values act on nothing. Every name in BOTH lists is
# validated against the registered mini-loops first, so a typo fails the deploy
# loudly before anything is touched (rather than silently mis-configuring the box).
apply_fleet_loop_policy() {
    local enabled_raw="${TEATREE_ENABLED_LOOPS-inbox}"
    local disabled_raw="${TEATREE_DISABLED_LOOPS-review}"
    local field loop registered intake
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

    # The owner-intake loops (single source of truth in Python) that must never be
    # forced off, so the owner's captured intent is always at least ingested (#3632).
    if ! intake="$(t3 loop intake-loops)"; then
        echo "entrypoint: could not read the owner-intake loop set ('t3 loop intake-loops' failed) - confirm the t3 install is healthy and re-run Deploy" >&2
        exit 1
    fi

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
        elif grep -qxF "$loop" <<<"$intake"; then
            echo "entrypoint: loop '${loop}' is an OWNER-INTAKE loop (interprets directives / delivers owner questions) - NOT forcing it off; the owner's captured intent must always be ingested, even under autonomous_away. Remove it from TEATREE_DISABLED_LOOPS in teatree.env to silence this warning" >&2
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

# True (0) when the box has working outbound connectivity to the git origin.
# It is the switch between the two boot modes the self-contained image supports
# (#3451): ONLINE fast-forwards the runtime clone from origin (self-update stays
# the in-loop `t3 update` path); OFFLINE runs the image's BAKED snapshot as-is,
# so a fresh box with only the image + secrets boots deterministically with zero
# fetches. `init_preflight` validates gh auth BEFORE this runs, so a non-zero
# `ls-remote` here is a genuine network fault, not a bad token (a bare
# reachability probe — no auth needed just to decide online/offline, and the
# public repo answers anonymously). `TEATREE_FORCE_OFFLINE=1|true|yes` forces the
# baked path for an operator who wants a pinned no-fetch boot, and is the seam the
# entrypoint smoke test drives to exercise both branches without real network.
network_up() {
    case "${TEATREE_FORCE_OFFLINE:-}" in
        1 | true | yes) return 1 ;;
    esac
    git ls-remote --quiet --exit-code "$REPO_URL" HEAD >/dev/null 2>&1
}

ensure_clone() {
    if [ -e "$CLONE_DIR/.git" ]; then
        if ! network_up; then
            # OFFLINE: run the baked snapshot as-is. The runtime clone was seeded
            # from the image's baked source (fresh box) or is a prior online
            # boot's clone, so the stack runs with zero fetches; the origin
            # fast-forward self-update below (and in-loop `t3 update`) resumes on
            # the next boot with connectivity.
            local baked_sha
            baked_sha="$(git -C "$CLONE_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
            echo "entrypoint: network unreachable - running the BAKED snapshot at $baked_sha (skipping origin fast-forward; self-update resumes when the network returns)" >&2
            return 0
        fi
        # ONLINE. The clone lives in a shared volume that outlives the image, so a
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
    # No runtime clone: an image built WITHOUT the #3451 bake stage (or an empty
    # teatree_src volume the baked source never seeded). Bootstrapping the source
    # from scratch needs the network; the published image bakes a clone here so a
    # fresh box never reaches this branch.
    if ! network_up; then
        echo "entrypoint: no runtime clone at $CLONE_DIR and the network is unreachable - cannot bootstrap the source offline (the published image bakes a clone here so a fresh box needs no first-boot fetch). Restore connectivity and re-run Deploy" >&2
        exit 1
    fi
    git clone "$REPO_URL" "$CLONE_DIR"
}

# Drain + 👀-ack inbound Slack on a cadence, SURFACING failures (#3443). The old
# `t3 slack check >/dev/null 2>&1 || true` swallowed every error, so a drain that
# could not boot Django looked identical to a healthy one and nobody ever saw it.
#
# `t3 slack check` exits 0 when it drained messages and 1 with NO output when the
# queue was empty (the common, healthy case on a quiet box) — so a non-zero exit
# is NOT itself a failure. A REAL failure is a non-zero exit that ALSO produced
# STDOUT (a Django boot traceback, a DB error). STDERR is captured SEPARATELY:
# every t3 invocation emits a benign WARNING there (an overlay's skills-root
# notice), so folding it into the emptiness test (2>&1) would misread every
# empty-queue poll as a failure. Real failures increment a consecutive-failure
# counter and log BOTH streams to stderr (visible in `docker compose logs
# teatree-slack-listener`); an empty-queue exit never does.
#
# Each pass rewrites a heartbeat file that `t3 doctor` reads from another
# container to surface a stuck/failed drain (self_heal `_check_slack_drain_alive`).
# The heartbeat path mirrors teatree.paths.DATA_DIR ($HOME/.local/share/teatree) —
# the filename is pinned to the doctor side by tests/test_deploy_slack_listener.py.
slack_drain_loop() {
    local interval="${SLACK_CHECK_INTERVAL_SECONDS:-15}"
    local heartbeat="${SLACK_DRAIN_HEARTBEAT:-$HOME/.local/share/teatree/slack-drain-heartbeat.json}"
    local consecutive=0 last_ok=null now out err rc errfile
    errfile="$(mktemp)"
    trap 'rm -f "$errfile"' EXIT
    mkdir -p "$(dirname "$heartbeat")"
    while true; do
        now="$(date +%s)"
        out="$(t3 slack check 2>"$errfile")" && rc=0 || rc=$?
        if [ "$rc" -eq 0 ] || { [ "$rc" -eq 1 ] && [ -z "$out" ]; }; then
            consecutive=0
            last_ok="$now"
        else
            consecutive=$((consecutive + 1))
            echo "entrypoint: slack drain (t3 slack check) FAILED rc=$rc (consecutive=$consecutive):" >&2
            printf '%s\n' "$out" >&2
            err="$(cat "$errfile")"
            [ -n "$err" ] && printf '%s\n' "$err" >&2
        fi
        printf '{"updated_at": %s, "interval_seconds": %s, "consecutive_failures": %s, "last_ok_at": %s}\n' \
            "$now" "$interval" "$consecutive" "$last_ok" >"$heartbeat"
        sleep "$interval"
    done
}

case "$ROLE" in
init)
    init_preflight
    ensure_clone
    # Resolve the interpreter + editable install + prek. The self-contained image
    # (#3451) BAKES all three (and seeds them onto the teatree_uv volume on a fresh
    # box), so this is a fast no-op refresh when online and is skipped entirely when
    # offline — first boot never cold-resolves the dependency graph from PyPI/astral.
    if network_up; then
        uv python install 3.13
        # The [slack] extra pulls slack_sdk so the slack-listener role's Socket-Mode
        # receiver can open its WebSocket. Without it `t3 slack listen` degrades to a
        # no-op ("slack_sdk not installed") and inbound Slack never reaches the loop.
        uv tool install --editable "${CLONE_DIR}[slack]" --reinstall --python 3.13
        # prek (the pre-commit reimplementation) is a DEV-group dependency, so the
        # editable tool install above does NOT provide it. Worktree provisioning
        # (`prek_hook.install`) and the base-clone commit/push gates need `prek` on
        # PATH; install it as a standalone uv tool (pinned to the lockfile) into the
        # shared teatree_uv volume so every role sees it. Runtime (not Dockerfile):
        # /opt/teatree/uv is a named volume that shadows any image-baked install.
        uv tool install prek==0.4.10
    else
        # OFFLINE: the interpreter, editable install, and prek are baked into the
        # image, so init proceeds with no cold fetch. Fail loud only if the image
        # was built WITHOUT the bake stage (no baked t3/prek to fall back on).
        echo "entrypoint: offline - using the baked interpreter + editable install + prek from the image (skipping the cold uv sync)" >&2
        for baked_tool in t3 prek; do
            command -v "$baked_tool" >/dev/null 2>&1 || {
                echo "entrypoint: offline and no baked '$baked_tool' on PATH - this image was built without the #3451 bake stage, so it cannot bootstrap offline. Restore connectivity and re-run Deploy" >&2
                exit 1
            }
        done
    fi
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
    # Provision the agent's ~/.claude/settings.json + `t3 setup` (skill links, the
    # t3@souliane plugin registration, statusLine, MCP). setup's statusLine writer
    # merges into (never clobbers) the file the seed writes (#3359).
    prepare_claude_runtime
    t3 teatree db migrate
    # Values are JSON: enum strings are quoted, booleans and ints are bare.
    seed_setting agent_harness '"claude_sdk"'
    seed_setting agent_runtime '"headless"'
    seed_setting loop_runner_enabled true
    # #3409/#3435: provision concurrency 0 = AUTO EQUALS the code default, so the
    # provenance-aware seeder intentionally SKIPS it — the runtime already
    # auto-derives from THIS host (nCPU/2, cgroup-aware), and the worker's compose
    # `cpus` cap is itself host-derived at deploy time (#3432) so that cgroup view
    # reflects the real host instead of a baked-in cap. `t3 doctor --repair` clears
    # ONLY a stale ENTRYPOINT-seeded pin, never an operator's deliberate one (#3434).
    seed_setting provision_max_concurrency 0
    seed_setting provision_ram_ceiling_percent 75
    seed_setting max_concurrent_local_stacks 1
    # The admin binds the box loopback (host networking), so auto-login fires for
    # the SSH-tunnelled 127.0.0.1 request — no admin password behind the tunnel.
    seed_setting admin_autologin_enabled true
    # Clear any drain-set quiescing flag so the FRESH worker RESUMES admission after a
    # rolling deploy (drain-then-deploy). This is a HARD `set false`, NOT a provenance
    # `seed`: `t3 worker drain` writes worker_quiescing via `config_setting set` (a
    # durable operator-style row), and a `seed false` — equal to the code default — is
    # a no-op that would leave the fresh worker quiesced and admitting nothing. NON-FATAL
    # like the seeds: a transient failure must not brick the stack (a warn, then the
    # operator can clear it via `t3 worker status` / `config_setting set`).
    if ! t3 teatree config_setting set worker_quiescing false; then
        echo "teatree-init: WARNING could not clear worker_quiescing ('t3 teatree config_setting set' failed); the worker may stay quiesced and admit no new work — clear it manually with 't3 teatree config_setting set worker_quiescing false' and check 't3 worker status'." >&2
    fi
    apply_fleet_loop_policy
    echo "teatree-init: complete"
    ;;
worker)
    # ~/.claude is per-container ephemeral, so the agent's plugin/skill registration
    # from init never reaches this container — re-run it here. For the WORKER, skills
    # are a HARD startup precondition: the loop spawns headless agents, and a worker
    # that spawns them with ZERO skills is the exact silent outage we refuse (owner
    # directive: PREFER HARD FAIL over running with a critical capability missing). So
    # `t3 setup` failing (set -e) OR the post-setup skills verification failing REFUSES
    # to start, loudly and specifically, rather than serving a skill-less loop.
    prepare_claude_runtime
    if ! verify_agent_skills; then
        echo "entrypoint: FATAL worker refusing to start: the t3 skills plugin is NOT registered (t3@souliane missing from ~/.claude/plugins/installed_plugins.json or not enabled in ~/.claude/settings.json) — the loop's agents would run SKILL-LESS. Re-run \`t3 setup\` in this container (or redeploy) and check \`t3 doctor check\`." >&2
        exit 1
    fi
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
    # gated by the worker singleton. `slack_drain_loop` backgrounds the cadence
    # (so `exec t3 slack listen` stays the foreground process), never trips
    # `set -e`, and — unlike the old `|| true` — logs real failures to stderr and
    # writes a heartbeat `t3 doctor` reads to catch a stuck/failed drain (#3443).
    #
    # ~/.claude is per-container ephemeral, so re-run the agent plugin/skill
    # registration here too (non-fatal — a listener must keep draining Slack even if
    # setup hiccups; init already proved setup works).
    prepare_claude_runtime || echo "entrypoint: WARNING prepare_claude_runtime failed in slack-listener - agent skills may be unavailable until restart" >&2
    slack_drain_loop &
    exec t3 slack listen
    ;;
admin)
    # ~/.claude is per-container ephemeral, so re-run the agent plugin/skill
    # registration here too (non-fatal — the admin UI must serve even if setup
    # hiccups; init already proved setup works).
    prepare_claude_runtime || echo "entrypoint: WARNING prepare_claude_runtime failed in admin - agent skills may be unavailable until restart" >&2
    # Bind the box loopback (the service uses host networking) so the SSH-tunnel
    # request arrives as 127.0.0.1 and clears the middleware's loopback check.
    exec t3 admin --host 127.0.0.1 --port 8000 --no-browser
    ;;
*)
    echo "entrypoint: unknown TEATREE_ROLE '$ROLE' (expected init|worker|admin|slack-listener|watchdog)" >&2
    exit 64
    ;;
esac
