#!/usr/bin/env bash
# teatree headless deployment entrypoint. One image, five roles selected by
# $TEATREE_ROLE:
#   init           — one-shot prep (clone + editable install + t3 setup + DB config),
#                    exits 0. worker/admin/slack-listener depend on its successful
#                    completion, so the editable-install-on-the-shared-clone happens once.
#   worker         — runs `t3 worker` (the loop cadence owner), DEBUG off.
#   admin          — runs `t3 admin` (Django admin under gunicorn, DEBUG off) on the box loopback.
#   slack-listener — runs `t3 slack listen` (the Socket-Mode receiver: records an inbound
#                    DM and wakes the answer cycle itself, and feeds the JSONL queue the
#                    worker's mention scanner drains). Only meaningful when an overlay is
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
#
# The script comes from the read-only checkout bind mount, never the image, so
# the watchdog runs the same revision the stack was deployed from. The path is
# the SAME variable the compose bind mount uses, defaulting to the box checkout;
# a hard-coded box path here would exec a file that exists on no other host.
if [ "$ROLE" = watchdog ]; then
    exec bash "${TEATREE_DEPLOY_CHECKOUT:-/home/teatree/teatree-deploy}/deploy/watchdog.sh" --loop
fi

CLONE_DIR="${TEATREE_CLONE_DIR:-/home/teatree/teatree}"
REPO_URL="${TEATREE_REPO_URL:-https://github.com/souliane/teatree.git}"

# HOST_ROOT is the fork root when core is VENDORED inside a downstream project
# (``<fork>/vendor/teatree``), and empty for a standalone core clone.
#
# It exists because overlays register through the ``teatree.overlays`` entry point
# of the HOST project, not of core. Installing core alone leaves that entry point
# unregistered, so `get_overlay("<overlay>")` raises "not found. Available:
# t3-teatree" and EVERY headless task on an overlay-owned ticket dies at dispatch —
# with no partial progress, which reads as a silent freeze rather than an error.
#
# Detected from the layout rather than configured, so a fork gets this right without
# knowing to set anything, and a standalone core clone is untouched (no vendor
# parent -> empty -> the install below is byte-identical to before).
#
# A FUNCTION, not a bare assignment, because `$CLONE_DIR` is the only input: every
# caller that has `$CLONE_DIR` can derive it, so no code path can reach a reference
# to an unset HOST_ROOT under `set -u`. `ensure_clone` derives its own copy rather
# than closing over the global for exactly that reason.
detect_host_root() {
    local candidate
    case "$CLONE_DIR" in
        */vendor/teatree)
            candidate="${CLONE_DIR%/vendor/teatree}"
            if [ -f "$candidate/pyproject.toml" ]; then
                printf '%s' "$candidate"
            fi
            ;;
    esac
}
HOST_ROOT="$(detect_host_root)"

# The filesystem type backing $1, resolved from the kernel's mount table by
# LONGEST matching mount point (a bind mount reports the transport that serves
# it, not the fs of any parent). Reading /proc/mounts is a pure kernel read — it
# never touches the directory itself, which is the whole point: the host's GPG
# home must be probed WITHOUT writing to it (see seed_container_gnupg_home).
# Unresolvable — no mount table, or a mount point whose path the kernel escaped
# (a space becomes `\040`, which cannot match) — yields the empty string, and the
# caller treats that as "not socket-capable", the safe direction.
# `TEATREE_PROC_MOUNTS` relocates the mount table, so the resolver can be driven
# against a fixture on a host that has no procfs.
path_fstype() {
    local target="$1" mounts="${TEATREE_PROC_MOUNTS:-/proc/mounts}" point type best_point="" best_type=""
    [ -r "$mounts" ] || return 0
    while read -r _ point type _; do
        case "$point" in
            /) ;;
            *)
                case "$target" in
                    "$point" | "$point"/*) ;;
                    *) continue ;;
                esac
                ;;
        esac
        if [ "${#point}" -ge "${#best_point}" ]; then
            best_point="$point"
            best_type="$type"
        fi
    done <"$mounts"
    printf '%s' "$best_type"
}

# True when $1 is a filesystem that can host a UNIX-DOMAIN SOCKET — the one
# capability gpg-agent and keyboxd need inside GNUPGHOME, since both bind their
# `S.*` sockets there.
#
# An ALLOWLIST of real local filesystems, deliberately, not a denylist of the
# bad ones: the failing set is every desktop/VM file-SHARING transport, and it is
# open-ended and renamed often (Docker Desktop alone has shipped osxfs,
# gRPC-FUSE, virtiofs and now `fakeowner`; Colima/Lima/OrbStack/Rancher add 9p,
# sshfs and more). An unknown name therefore takes the DERIVE path, which works
# on every filesystem, rather than the in-place path, which fails on exactly the
# names we could not enumerate.
fstype_hosts_unix_sockets() {
    case "$1" in
        ext2 | ext3 | ext4 | xfs | btrfs | zfs | f2fs | jfs | reiserfs | overlay | overlayfs | tmpfs | ramfs) return 0 ;;
        *) return 1 ;;
    esac
}

# Copy the material a container-local GPG home needs to DECRYPT the pass store
# from the host mount $1 into $2. An explicit ALLOWLIST rather than a
# copy-everything-then-prune: it cannot accidentally carry over a stale socket or
# a leaked lock, and it states exactly what the derived home is made of.
#
#   common.conf              carries `use-keyboxd`. COPIED, deliberately — on a
#                            keyboxd host the public keys live ONLY in
#                            public-keys.d/pubring.db, so DROPPING the directive
#                            would send gpg looking for a pubring.kbx that does
#                            not exist and it would find zero keys.
#   gpg.conf                 the operator's own gpg options (default-key, trust-model).
#   pubring.kbx / .gpg       the public keyring on a host that predates keyboxd.
#   trustdb.gpg              ownertrust — without it `pass insert` refuses to encrypt.
#   private-keys-v1.d/*.key  the secret keys themselves.
#   public-keys.d/pubring.db the keyboxd public keyring.
#
# Deliberately NOT copied: `S.*` (the host's agent/keyboxd sockets — copying the
# very things that make the mount unusable would defeat the exercise), `*.lock`
# and `.#lk*` (dotlocks leaked by a process that died holding them), `random_seed`
# (a per-machine entropy pool gpg regenerates), `openpgp-revocs.d` (revocation
# certificates, irrelevant to decryption), and gpg-agent.conf / scdaemon.conf /
# dirmngr.conf — host DAEMON configs that routinely name host-only binaries
# (`pinentry-program /opt/homebrew/bin/pinentry-mac`) which do not exist in this
# image; the container's own defaults are the headless-correct ones.
derive_container_gnupg_home() {
    local source="$1" derived="$2" name
    rm -rf "$derived"
    mkdir -p "$derived" || return 1
    chmod 700 "$derived"
    for name in common.conf gpg.conf pubring.kbx pubring.gpg trustdb.gpg; do
        if [ -f "$source/$name" ]; then
            cp -p "$source/$name" "$derived/$name"
        fi
    done
    if [ -d "$source/private-keys-v1.d" ]; then
        mkdir -p "$derived/private-keys-v1.d"
        chmod 700 "$derived/private-keys-v1.d"
        find "$source/private-keys-v1.d" -maxdepth 1 -type f -name '*.key' \
            -exec cp -p {} "$derived/private-keys-v1.d/" \;
    fi
    if [ -f "$source/public-keys.d/pubring.db" ]; then
        mkdir -p "$derived/public-keys.d"
        chmod 700 "$derived/public-keys.d"
        cp -p "$source/public-keys.d/pubring.db" "$derived/public-keys.d/pubring.db"
    fi
    return 0
}

# Seed the container's ONE GPG home — a FIXED path every entry point already agrees on.
#
# THE FAILURE THIS SHAPE FORECLOSES. GNUPGHOME used to be resolved per process: the image
# baked the host mount, and only this entrypoint's own tree got the corrected value. A
# `docker exec` — the loop's own subprocesses included — started from the container's
# create-time environment and reached for the HOST home instead, where two things go
# wrong at once. gpg-agent and keyboxd bind their `S.*` sockets inside GNUPGHOME, which a
# file-sharing transport (Docker Desktop for Mac: `fakeowner`) cannot host at all, so
# keyboxd dies and gpg finds zero keys; and gpg's dotlock records `pid` + `hostname`, so a
# lock the HOST's own keyboxd holds on the shared keybox names a hostname no container can
# match — gpg will not judge it stale and waits forever. Both disappear once no container
# process ever opens the host home. Patching each entry point separately (a login-shell
# profile, a wrapper prologue) left every unpatched one broken, which is the class.
#
# The host home stays strictly READ-ONLY: it is the SOURCE, never GNUPGHOME. Detection
# reads /proc/mounts, so even it never touches the directory.
#
# A socket-capable host home is ADOPTED in place through a symlink, so the box keeps its
# exact current behaviour — one home shared by every service means one gpg-agent, which is
# what makes the cached-passphrase setup deploy/README.md documents work.
CONTAINER_GNUPG_HOME="${TEATREE_GNUPG_RUNTIME_DIR:-/home/teatree/.gnupg-run}/gnupg"

seed_container_gnupg_home() {
    local source="${TEATREE_HOST_GNUPG_DIR:-/home/teatree/.gnupg}" fstype
    export GNUPGHOME="$CONTAINER_GNUPG_HOME"
    [ -d "$source" ] || return 0
    fstype="$(path_fstype "$source")"
    if fstype_hosts_unix_sockets "$fstype"; then
        # The parent is a compose tmpfs in the deployed stack, but a bare `docker run`
        # of this image has none — and `set -e` would turn a failing `ln` into a dead
        # container rather than a degraded gpg. Symmetric with the copy path's mkdir -p.
        if ! mkdir -p "$(dirname "$CONTAINER_GNUPG_HOME")"; then
            echo "entrypoint: WARN could not create $(dirname "$CONTAINER_GNUPG_HOME") - gpg reads will fail" >&2
            return 0
        fi
        rm -rf "$CONTAINER_GNUPG_HOME"
        ln -s "$source" "$CONTAINER_GNUPG_HOME"
        echo "entrypoint: GNUPGHOME $CONTAINER_GNUPG_HOME adopts the host GPG home $source in place (on '$fstype', which hosts the gpg-agent/keyboxd sockets)"
        return 0
    fi
    if ! derive_container_gnupg_home "$source" "$CONTAINER_GNUPG_HOME"; then
        echo "entrypoint: WARN the host GPG home $source is on '$fstype' (cannot host the gpg-agent/keyboxd sockets) and a container-local copy at $CONTAINER_GNUPG_HOME could not be created - gpg reads will fail" >&2
        return 0
    fi
    # Absence stays a no-op: a host with no key material yields an empty derived home,
    # gpg finds no keys exactly as before, and init_preflight reports the same message.
    echo "entrypoint: GNUPGHOME $CONTAINER_GNUPG_HOME holds a container-local copy of $source (on '$fstype', which cannot host the gpg-agent/keyboxd sockets; the host GPG home is left untouched)"
}
seed_container_gnupg_home

# gpg refuses a group/other-readable home, so normalise GNUPGHOME's mode BEFORE
# the boot-time `pass show` reads below can decrypt — only when the mount is
# writable (a hardened read-only mount would EROFS here under -e) AND the mode is
# not already right, so the common case writes NOTHING to the host's GPG home.
if [ -n "${GNUPGHOME:-}" ] && [ -d "$GNUPGHOME" ] && [ -w "$GNUPGHOME" ] &&
    [ "$(stat -c %a "$GNUPGHOME" 2>/dev/null || echo 700)" != 700 ]; then
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

# A GitHub bootstrap read is opt-in. Runtime routing belongs to the owning overlay's
# ``github_token_pass_key`` DB setting, and a guessed pass path would shadow it.
if [ -n "${TEATREE_GH_TOKEN_PASS_PATH:-}" ]; then
    source_secret_from_pass TEATREE_GH_TOKEN "$TEATREE_GH_TOKEN_PASS_PATH"
fi
# The admin password is deployment infrastructure rather than an overlay credential.
source_secret_from_pass T3_ADMIN_PASSWORD "${T3_ADMIN_PASSWORD_PASS_PATH:-teatree/admin-password}"
# The Notion token is exported only from an entry named here: an exported value beats the
# notion_token_pass_key setting, so a guessed default would shadow the entry the venue routes.
if [ -n "${NOTION_TOKEN_PASS_PATH:-}" ]; then
    source_secret_from_pass NOTION_TOKEN "$NOTION_TOKEN_PASS_PATH"
fi

if [ "$ROLE" = "init" ] && [ -n "${TEATREE_GH_TOKEN:-}" ]; then
    export GH_TOKEN="$TEATREE_GH_TOKEN"
fi

# Configure git to use gh as the https credential helper for EVERY role (idempotent):
# the worker/admin `git push` over https needs it too, not just the init clone.
if [ "$ROLE" = "init" ] && [ -n "${GH_TOKEN:-}" ]; then
    gh auth setup-git
fi

# GitHub env tokens are an init-only bootstrap for the permission preflight and
# first clone. Every long-running role resolves the owning overlay's DB route.
if [ "$ROLE" != "init" ]; then
    unset GH_TOKEN GITHUB_TOKEN TEATREE_GH_TOKEN
fi

# The GitLab TOKEN half. The credential HELPER that consumes it is baked into the
# image (deploy/Dockerfile, `git config --system`) because it carries no secret;
# only the token is runtime state, and only it belongs here. Without this the
# container authenticates to GitHub but not to GitLab, so provisioning cannot clone
# a private overlay repo into its own workspace volume — every clone dies on
# "HTTP Basic: Access denied" while the operator's host glab is logged in the whole
# time.
#
# This export reaches only THIS role's process tree. A `docker exec` starts from the
# container's create-time environment and never sees it, so the compose files declare
# GITLAB_TOKEN per service and the deploying host may resolve it from the same explicitly
# configured bootstrap entry — that declaration, not this read, is what an exec'd process
# inherits. With no explicit entry, never guess a legacy path that can disagree with the
# DB-bound route. A value compose already supplied still wins.
if [ -z "${GITLAB_TOKEN:-}" ] && [ -n "${TEATREE_GITLAB_TOKEN_PASS_PATH:-}" ]; then
    source_secret_from_pass TEATREE_GITLAB_TOKEN "$TEATREE_GITLAB_TOKEN_PASS_PATH"
    if [ -n "${TEATREE_GITLAB_TOKEN:-}" ]; then
        export GITLAB_TOKEN="$TEATREE_GITLAB_TOKEN"
    fi
fi

# Global git identity fallback — commits and the runtime loop need one.
git config --global user.name "${GIT_AUTHOR_NAME:-teatree}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-teatree@localhost}"
git config --global init.defaultBranch main
git config --global --add safe.directory "$CLONE_DIR"

# Point clone discovery at a checkout every venue can reach.
#
# `git worktree add` bakes an ABSOLUTE `gitdir:` pointer into its SOURCE CLONE,
# so the clone's path — not the worktree's — decides who can use the result. The
# image links `~/workspace/souliane/teatree` at the `teatree_src` volume, a path
# that exists nowhere but this container, so a worktree cut here answered
# `fatal: not a git repository` from the host even though its files were readable
# through the `t3-workspaces` bind (#4120). The deploy checkout is bind-mounted at
# path identity, so pointing the link there makes the recorded pointer portable.
#
# Degrades to the image's link rather than failing: with nothing exported dockerd
# creates an empty dir at the default path, and a container that provisions
# container-only worktrees is still better than one that cannot provision at all.
retarget_clone_discovery() {
    local link="${1:-/home/teatree/workspace/souliane/teatree}"  # privacy-scan:allow — the box's public, documented deploy home
    local checkout="${TEATREE_DEPLOY_CHECKOUT:-}"
    if [ -z "$checkout" ] || ! git -C "$checkout" rev-parse --git-dir >/dev/null 2>&1; then
        echo "entrypoint: no git checkout at TEATREE_DEPLOY_CHECKOUT='${checkout}' - leaving clone discovery on the image link; worktrees cut here resolve only inside this container (#4120)" >&2
        return 0
    fi
    # A real directory is someone's clone, not the image's link — never clobber it.
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        echo "entrypoint: $link is a real directory, not the image's discovery link - leaving it untouched (#4120)" >&2
        return 0
    fi
    mkdir -p "$(dirname "$link")"
    ln -sfn "$checkout" "$link"
}

retarget_clone_discovery

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

    # RECOMMENDED (WARN-tier) probes — never exit 1. workflows:write is never actively
    # probed for a fine-grained token (see above), so it is NOT seeded here: "no
    # reliable probe exists" is not evidence the permission is absent. Seeding it made
    # every deploy tell the operator to recreate a token that may already carry it,
    # with no action able to clear the warning. It rides along on the recreate line
    # only when a REAL gap already means a recreate.
    warn_missing=()
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
        echo "entrypoint: WARN TEATREE_GH_TOKEN is missing recommended permission(s): ${warn_missing[*]} - these degrade optional features (CI trigger/status, auto-merge's required-checks rollup, the CI OAuth-account switch) but do NOT block boot. Fine-grained tokens cannot be widened via the API either - recreate it with these permissions added: $_GH_FINE_GRAINED_TOKENS_URL. While recreating, also include 'workflows: write': it cannot be probed on a fine-grained token, so its presence is unknown here - a push touching .github/workflows/* is what proves it either way." >&2
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

# Reconcile the factory-owned Claude, Codex, and universal agent homes. Compose
# mounts all three as named volumes shared by init and runtime roles, so init is
# the one writer and `t3 setup` is the sole source of installed factory skills.
# Runtime roles consume the completed state after depends_on(init) and never race
# each other by running setup concurrently.
prepare_agent_homes() {
    seed_claude_settings
    t3 setup --strict-agent-skills
}

# VERIFY the agent's skills are actually available after `prepare_agent_homes`:
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
    [ -n "$install_path" ] && [ -d "$install_path" ] || return 1
    local ready="$HOME/.local/share/teatree/skills/ready"
    [ "$(cat "$ready" 2>/dev/null)" = "v1" ]
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

# `uv tool install --reinstall` DELETES the working tool venv before rebuilding it, so a
# filesystem that fills mid-build leaves neither install: #4338 measured 391 MB free, 124
# packages written, `click` absent, and every CLI invocation dead at `import typer` with
# the worker crash-looping for 13 hours. Refusing the boot leaves the PREVIOUS venv intact,
# which is recoverable; proceeding is not.
#
# Measure the filesystem holding the UV TOOL DIR, not `/`: /opt/teatree/uv is a named
# volume and may be a different device, which would make a `df /` gate vacuous or
# spuriously firing. An unmeasurable filesystem PROCEEDS - an absent reading is not
# evidence of no room. The floor mirrors `teatree.utils.install_headroom`'s Python default;
# `tests/test_deploy_entrypoint_install_headroom.py` pins the two to the same number.
require_install_headroom() {
    local floor target free
    floor="${TEATREE_INSTALL_MIN_FREE_MB:-2048}"
    target="$(uv tool dir 2>/dev/null || true)"
    [ -n "$target" ] || target="${UV_TOOL_DIR:-$HOME/.local/share/uv/tools}"
    while [ ! -d "$target" ] && [ "$target" != "/" ] && [ "$target" != "." ]; do
        target="$(dirname "$target")"
    done
    free="$(df -Pm "$target" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [ -z "$free" ]; then
        echo "entrypoint: WARNING could not measure free space on '$target' - proceeding with the reinstall" >&2
        return 0
    fi
    if [ "$free" -lt "$floor" ]; then
        echo "entrypoint: FATAL refusing the destructive editable reinstall: ${free} MB free on '${target}', floor ${floor} MB (TEATREE_INSTALL_MIN_FREE_MB)." >&2
        echo "entrypoint: the previous tool venv is left INTACT - reclaim space, then restart this container:" >&2
        echo "entrypoint:   docker system prune -f              # reclaim image and build cache" >&2
        exit 1
    fi
}

# The clone's own lockfile, rendered as a constraints file for `uv tool install`.
#
# `uv tool install --reinstall` RE-RESOLVES from the index and reads no lockfile — the
# working directory makes no difference — so the deployed container has always run a
# dependency graph that no CI lane ever resolved. That is not theoretical: gunicorn
# 26.1.0 dropped its `packaging` dependency, `packaging` was reaching the tool env only
# through that edge, and `teatree.utils.dep_skew` imports it on the `t3 doctor` path. The
# doctor plane was down for 72 consecutive watchdog passes. `uv.lock` still pinned
# gunicorn 26.0.0 the whole time, so every CI and dev venue stayed green.
#
# `--no-default-groups` is BOOT-SAFETY, not tidiness: an export carrying the dev group
# pins `prek`, and the `uv tool install prek==<pin>` below then dies "your requirements
# are unsatisfiable" under the image's ambient UV_CONSTRAINT — a bricked boot, strictly
# worse than the bug. `--all-extras` keeps `[slack]` (the extra this role installs) in
# scope. `--frozen` reads the committed lock and never re-resolves it, so this is
# network-free and runs on the offline path too.
#
# A failed export writes a COMMENT-ONLY file rather than none: `uv tool install` errors
# outright on a missing `--constraints` path, so the fallback has to be a file that
# constrains nothing. The install then degrades to today's unconstrained resolve — the
# bug — instead of taking the boot down with it.
CONSTRAINTS_FILE="${CLONE_DIR}/uv-constraints.txt"

# The image's `ENV UV_CONSTRAINT` is a BUILD-time expansion of $TEATREE_CLONE_DIR, so a fork
# that vendors core under vendor/teatree — and overrides that variable at RUNTIME — inherits
# a path nothing ever writes. uv errors outright on a missing constraints file, so the
# unflagged `uv tool install prek` below exits 2 and every role gated on init stays `Created`
# (#4659). Realigned HERE, not inside ensure_uv_constraints: that has one call site, in the
# init role, while worker/slack-listener/admin install off the same ambient value.
export UV_CONSTRAINT="$CONSTRAINTS_FILE"

ensure_uv_constraints() {
    local tmp="${CONSTRAINTS_FILE}.tmp"
    if uv export --no-hashes --no-emit-project --frozen --no-default-groups --all-extras \
        --directory "$CLONE_DIR" -o "$tmp" >/dev/null 2>&1 && [ -s "$tmp" ]; then
        mv -f "$tmp" "$CONSTRAINTS_FILE"
        echo "entrypoint: lockfile constraints regenerated at $CONSTRAINTS_FILE ($(grep -cE '^[a-zA-Z0-9]' "$CONSTRAINTS_FILE") pins)" >&2
        return 0
    fi
    rm -f "$tmp"
    echo "entrypoint: WARNING could not export $CLONE_DIR/uv.lock as constraints - the install will RE-RESOLVE from the index (see #4049 class: an undeclared transitive dep can vanish under you)" >&2
    printf '# uv export failed at boot - no constraints applied.\n' >"$CONSTRAINTS_FILE"
}

ensure_clone() {
    # VENDORED SUBTREE: core lives inside a downstream fork (`detect_host_root`
    # non-empty). The source is already on disk and is NOT a git clone —
    # a subtree directory carries no `.git` of its own — so every branch below is
    # wrong for it, and dangerously so:
    #
    #   * the `-e "$CLONE_DIR/.git"` test fails, so control falls through to
    #     `git clone "$REPO_URL" "$CLONE_DIR"`, which errors on a non-empty directory.
    #     That is why `init` has never completed on a fork.
    #   * worse, were it to succeed it would clone PUBLIC upstream OVER the fork's
    #     vendored core, silently replacing the deployed code with someone else's.
    #
    # A fork's tree is shipped whole by its own deploy (the CI job replaces the
    # checkout), so there is nothing to fetch or fast-forward here. Upstream is taken
    # by a deliberate, reviewed vendor bump — never by a boot-time pull.
    local host_root
    host_root="$(detect_host_root)"
    if [ -n "$host_root" ]; then
        echo "entrypoint: source is a vendored subtree under $host_root - already present, skipping clone/self-update (the fork's own deploy ships the tree)" >&2
        return 0
    fi
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
            if git -C "$CLONE_DIR" merge-base --is-ancestor HEAD "origin/$default_branch"; then
                echo "entrypoint: $CLONE_DIR could not fast-forward '$default_branch' for a non-history reason - read the Git error above (for example an index lock or permission problem), fix it, and re-run Deploy" >&2
            else
                local ancestry_status=$?
                if [ "$ancestry_status" -eq 1 ]; then
                    echo "entrypoint: $CLONE_DIR default branch '$default_branch' has diverged (local commits that cannot fast-forward) - reconcile it on the box and re-run Deploy" >&2
                else
                    echo "entrypoint: $CLONE_DIR could not fast-forward '$default_branch', and Git could not determine its ancestry - read the Git errors above, fix them, and re-run Deploy" >&2
                fi
            fi
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

# Refuse to publish a `$CLONE_DIR` that is not teatree core into the SHARED uv
# volume. `uv tool install --editable "$CLONE_DIR"` writes `/opt/teatree/uv`,
# which every role reads, so a `$CLONE_DIR` naming the wrong tree does not fail
# THIS container — it bricks all of them: pointed at a fork ROOT (the default
# whenever a deploy loses TEATREE_CLONE_DIR) it publishes a `teatree.pth` naming
# a directory that holds neither `teatree` nor `t3_bootstrap`, and every `t3` on
# the box then dies with `ModuleNotFoundError: No module named 't3_bootstrap'` —
# including the one an operator would reach for to diagnose it. The two modules
# checked here are exactly what the `t3` console script imports.
assert_core_source() {
    local missing=""
    [ -f "$CLONE_DIR/src/teatree/__init__.py" ] || missing="src/teatree/__init__.py"
    [ -f "$CLONE_DIR/src/t3_bootstrap/__init__.py" ] || missing="${missing:+$missing, }src/t3_bootstrap/__init__.py"
    [ -z "$missing" ] || {
        echo "entrypoint: TEATREE_CLONE_DIR='$CLONE_DIR' is not a teatree core source tree (missing: $missing) - refusing the editable install, which would publish a broken 't3' into the shared /opt/teatree/uv volume for EVERY container. A fork vendoring core must export TEATREE_CLONE_DIR=<mount>/vendor/teatree (deploy/t3 and deploy/deploy.sh do; a bare 'docker compose up' does not)." >&2
        exit 1
    }
}

case "$ROLE" in
init)
    init_preflight
    ensure_clone
    # Do not let setup/runtime reads inherit the bootstrap identity. From here on,
    # GitHub credentials are bound to the repository's owning overlay.
    unset GH_TOKEN GITHUB_TOKEN TEATREE_GH_TOKEN
    assert_core_source
    # Before ANY uv install: the image exports UV_CONSTRAINT at this path, and uv errors
    # outright when a constraints file is missing, so it must exist for every role that
    # later runs `t3 update` off this shared volume.
    ensure_uv_constraints
    # The TOOL plane, named explicitly rather than inherited (#4642). Compose points
    # the ambient UV_PYTHON_INSTALL_DIR at the shared PROJECT root, so a bare
    # `uv tool install --reinstall` below would rebuild the tool venvs against a
    # host-controlled directory and couple this container's own `t3` and `prek` to
    # it. Pinning the volume keeps a tool venv rebuilt where it was baked.
    TOOL_PYTHON_ROOT=/opt/teatree/uv/python
    # Resolve the interpreter + editable install + prek. The self-contained image
    # (#3451) BAKES all three (and seeds them onto the teatree_uv volume on a fresh
    # box), so this is a fast no-op refresh when online and is skipped entirely when
    # offline — first boot never cold-resolves the dependency graph from PyPI/astral.
    if network_up; then
        uv python install 3.13
        # The [slack] extra pulls slack_sdk so the slack-listener role's Socket-Mode
        # receiver can open its WebSocket. Without it `t3 slack listen` degrades to a
        # no-op ("slack_sdk not installed") and inbound Slack never reaches the loop.
        # SDK 0.2.152's `mcp<3.0.0` admits teatree's `mcp>=2,<3`; the empty
        # `--overrides` plumbing stays explicit because uv ignores the package's own table.
        # `--constraints` is the LOCKFILE bound (see ensure_uv_constraints): without it
        # this `--reinstall` re-resolves the whole graph from the index and can install
        # versions no CI lane has ever run.
        # --with-editable registers the HOST project's `teatree.overlays` entry point
        # alongside core. Without it a vendored fork installs core only, every overlay
        # resolves to "not found", and each headless task on an overlay ticket dies at
        # dispatch. Empty for a standalone core clone, where `set --` expands to nothing
        # and this is the original single-package install.
        set -- ${HOST_ROOT:+--with-editable "$HOST_ROOT"}
        require_install_headroom
        UV_PYTHON_INSTALL_DIR="$TOOL_PYTHON_ROOT" \
            uv tool install --editable "${CLONE_DIR}[slack]" "$@" --reinstall --python 3.13 \
            --overrides "${CLONE_DIR}/uv-overrides.txt" \
            --constraints "${CONSTRAINTS_FILE}"
        # An install that produced a venv whose CLI cannot start is an install FAILURE, not
        # a later mystery (#4338). The console script is `t3_bootstrap:main` -> `from
        # teatree.cli import main`, so `--help` exercises the whole import chain - the exact
        # `import typer` death - while touching no DB, config or network. Adjacent to the
        # install so the error names its origin instead of surfacing downstream as a
        # confusing traceback in an unrelated step.
        if ! t3 --help >/dev/null 2>&1; then
            echo "entrypoint: FATAL the editable install completed but the CLI does not run (\`t3 --help\` fails) - the tool venv is incomplete: a truncated install leaves declared dependencies missing, e.g. typer without click. Reclaim disk and restart this container." >&2
            exit 1
        fi
        # prek (the pre-commit reimplementation) is a DEV-group dependency, so the
        # editable tool install above does NOT provide it. Worktree provisioning
        # (`prek_hook.install`) and the base-clone commit/push gates need `prek` on
        # PATH; install it as a standalone uv tool (pinned to the lockfile) into the
        # shared teatree_uv volume so every role sees it. Runtime (not Dockerfile):
        # /opt/teatree/uv is a named volume that shadows any image-baked install.
        UV_PYTHON_INSTALL_DIR="$TOOL_PYTHON_ROOT" uv tool install prek==0.4.10
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
    # The interpreter root is now a bind of the HOST's (#4642), so unlike the
    # image-baked volume it can arrive EMPTY — a fresh box whose host has never
    # run uv, booting offline, where the install above was skipped. Every later
    # `uv run` would then rebuild every worktree venv it touches. Name the gap
    # instead: this cannot self-heal without the network, and a refusal stating
    # the remedy beats a silent gigabyte. Outside the network guard on purpose.
    if ! ls -d "$UV_PYTHON_INSTALL_DIR"/cpython-* >/dev/null 2>&1; then
        echo "entrypoint: FATAL the shared interpreter root $UV_PYTHON_INSTALL_DIR holds no cpython-* interpreter. It is a bind of the host's uv python root, so it is empty until something installs there. Run 'uv python install $(cat "${CLONE_DIR}/.python-version")' on the host (or restore connectivity and re-run Deploy), then restart this container." >&2
        exit 1
    fi
    # Install the commit/push gate hooks on the base clone's SHARED hooks dir
    # (git links every worktree to it), so the privacy leak gate (#685), the
    # foreign-MR guard, banned-terms, and the push gates actually fire on the
    # loop's pushes. Without this the migrated box had an EMPTY .git/hooks and
    # every gate was silently bypassed. Idempotent.
    #
    # The baked PREK path is deliberately NOT rewritten here: this dir is shared with
    # the host, and the unprobed PATH lookup this used to write resolved there to a
    # bind-mounted host venv that dies `Exec format error`. `prepare_agent_homes`
    # below runs `t3 setup`, whose `harden_hooks` probes candidates and reaches this
    # very dir — the installed-clone walk passes `.git`-less `vendor/teatree` up to
    # the fork root, whose common git dir is the one `prek install` just wrote.
    (cd "$CLONE_DIR" && prek install -f)
    # Provision the agent's ~/.claude/settings.json + `t3 setup` (skill links, the
    # t3@souliane plugin registration, statusLine, MCP). setup's statusLine writer
    # merges into (never clobbers) the file the seed writes (#3359).
    prepare_agent_homes
    t3 teatree db migrate
    # Values are JSON: enum strings are quoted, booleans and ints are bare.
    seed_setting agent_harness '"claude_sdk"'
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
    echo "teatree-init: complete"
    ;;
worker)
    # Init owns setup on the shared agent-home volumes. The worker consumes that
    # exact state and hard-fails if it is absent rather than silently spawning a
    # skill-less agent.
    if ! verify_agent_skills; then
        echo "entrypoint: FATAL worker refusing to start: strict agent-skill setup is incomplete (Claude plugin, Codex plugin, declared skills, inventory, or readiness marker). Re-run \`t3 setup --strict-agent-skills\` in this container (or redeploy) and check \`t3 doctor check\`." >&2
        exit 1
    fi
    exec t3 worker
    ;;
slack-listener)
    # Socket-Mode receiver: one WebSocket per slack-enabled overlay, writing
    # inbound events to the JSONL queue that the worker's mention scanner
    # drains. `t3 slack listen` exits non-zero
    # when no overlay is Slack-enabled; `restart: unless-stopped` then simply
    # keeps a harmless retry loop on a box that has no Slack overlay yet.
    #
    # The receiver itself now records each inbound DM and wakes the answer cycle,
    # so nothing here needs a `t3 slack check` cadence to make a capture observable;
    # the receiver also stamps the heartbeat `t3 doctor` reads.
    #
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
