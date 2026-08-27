# shellcheck shell=bash
# Interpreter resolution for the shell hooks — sourced, never executed.
#
# Two functions, both on the hook hot path (the banned-terms gate runs on every
# commit, the leak gate on every push):
#
#   resolve_uv                          -> prints a uv that ANSWERS, or fails
#   uv_project_run_prefix <uv> <project> -> sets the UV_PROJECT_RUN array
#
# `resolve_uv`'s two failure codes are a CONTRACT, because they call for opposite
# remedies and a caller that collapses them sends the reader the wrong way:
#
#   1 -> candidates were listed and none of them ran. A real resolution problem:
#        every uv here is a shim, or there is none. Installing one helps.
#   2 -> the LISTING ITSELF did not finish, so "none of them ran" was never
#        established. The box's uv is not implicated and reinstalling it cannot
#        help; the fault is in this script's own execution — a shell lacking a
#        feature it uses, an unset-variable trap, a permission error mid-walk.
#
# Code 2 earns its keep: without it a bash 4+ builtin in the listing, absent under
# the bash 3.2 macOS selects, returned an empty list, and the gate reported a
# missing uv while uv sat installed and healthy — a fail-closed gate misdiagnosing
# its own crash and prescribing a fix that could not work.
#
# `command -v uv` is not an answer. Under a version manager the PATH entry is a
# SHIM that picks its interpreter from the CWD repo's `.python-version`, and the
# hook's CWD is an arbitrary repo — so an uninstalled pin makes the shim exit 127
# before uv is ever reached, while uv itself is installed and healthy. Candidates
# are therefore PROBED, natives (no `#!`) first, and a shim is only accepted when
# nothing native answers. `T3_UV` overrides everything and is never cached.
#
# The probe answer is memoised under the cache home, keyed on the candidate SET,
# so the steady state spawns nothing and a newly installed or removed uv is still
# picked up with nobody clearing anything. The ENVIRONMENT decision below is
# deliberately NOT memoised — see `uv_project_run_prefix`.

# The environment a hook falls back to when uv would otherwise reconcile one that
# is not ours. Relative, so uv resolves it against the same workspace root.
_UV_HOOK_ENV_NAME=".venv-hook"

# How far up from the project a `.venv` still counts as "at or above" it, and how
# far up a `pyproject.toml` is still an ancestor of this project.
_UV_ENV_SEARCH_DEPTH=5

# Printed as the last line of a candidate walk that ran to the end. Its ABSENCE is
# the only signal separating "no uv here" from "the walk crashed" — see resolve_uv.
_UV_CANDIDATES_COMPLETE="__uv_candidates_complete__"

_uv_cache_file() {
    printf '%s' "${XDG_CACHE_HOME:-${HOME:-/nonexistent}/.cache}/teatree/uv-resolved"
}

_uv_is_native() {
    # A compiled binary, as opposed to a `#!` wrapper (a version-manager shim).
    [ "$(head -c2 "$1" 2>/dev/null || true)" != '#!' ]
}

_uv_answers() {
    "$1" --version >/dev/null 2>&1
}

_uv_version_ordered() {
    # `<root>/<version>/bin/uv` trees, newest version FIRST.
    local path version
    local -a rows=()
    for path in "$@"; do
        [ -x "$path" ] || continue
        version="$(basename "$(dirname "$(dirname "$path")")")"
        rows+=("${version}|${path}")
    done
    [ ${#rows[@]} -eq 0 ] && return 0
    printf '%s\n' "${rows[@]}" | sort -t'|' -k1,1Vr | cut -d'|' -f2-
}

_uv_candidates() {
    # Every uv worth probing, in preference order, deduplicated. Version-manager
    # trees come first so a managed install beats the standalone one, and the PATH
    # entry comes last because it is the one most likely to be a shim.
    #
    # The list is collected with a `read` loop rather than `mapfile`, a bash 4+
    # builtin: macOS ships bash 3.2 as `/bin/bash`, and `#!/usr/bin/env bash`
    # selects it whenever a newer one is not earlier on PATH. There the builtin
    # is simply absent, so the list comes back EMPTY and the gates consuming it
    # refuse every commit while blaming a uv that is installed and healthy.
    # Array expansions are `:-` guarded for the same reason: that bash treats an
    # empty array as unset under `set -u`.
    local home="${HOME:-/nonexistent}"
    local -a ordered=()
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        ordered+=("$line")
    done < <(
        _uv_version_ordered "${PYENV_ROOT:-${home}/.pyenv}"/versions/*/bin/uv
        _uv_version_ordered "${ASDF_DATA_DIR:-${home}/.asdf}"/installs/uv/*/bin/uv
        printf '%s\n' "${home}/.local/bin/uv" "${home}/.cargo/bin/uv"
        type -a -P uv 2>/dev/null || true
    )

    local -a seen=() natives=() wrappers=()
    for path in "${ordered[@]:-}"; do
        [ -n "$path" ] && [ -x "$path" ] || continue
        case " ${seen[*]:-} " in *" ${path} "*) continue ;; esac
        seen+=("$path")
        if _uv_is_native "$path"; then natives+=("$path"); else wrappers+=("$path"); fi
    done
    [ ${#natives[@]} -gt 0 ] && printf '%s\n' "${natives[@]}"
    [ ${#wrappers[@]} -gt 0 ] && printf '%s\n' "${wrappers[@]}"
    # Why a marker and not an exit status: this runs in a process substitution,
    # whose status the reading loop never observes. Reaching this line is the only
    # evidence the walk finished.
    printf '%s\n' "$_UV_CANDIDATES_COMPLETE"
    return 0
}

_uv_write_cache() {
    # Best-effort: a read-only cache home must never fail a resolution.
    local file="$1" key="$2" answer="$3"
    mkdir -p "$(dirname "$file")" 2>/dev/null || return 0
    printf '%s\n%s\n' "$key" "$answer" >"$file" 2>/dev/null || return 0
}

resolve_uv() {
    local override="${T3_UV:-}"
    if [ -n "$override" ] && [ -x "$override" ]; then
        printf '%s' "$override"
        return 0
    fi

    local -a candidates=()
    local candidate complete=0
    while IFS= read -r candidate || [ -n "$candidate" ]; do
        if [ "$candidate" = "$_UV_CANDIDATES_COMPLETE" ]; then
            complete=1
            continue
        fi
        candidates+=("$candidate")
    done < <(_uv_candidates)
    [ "$complete" -eq 1 ] || return 2

    # The candidate SET is the key: a uv installed or removed since the last run
    # changes it, so the stale answer is never handed back.
    local key cache_file
    key="${candidates[*]:-}"
    cache_file="$(_uv_cache_file)"
    if [ -f "$cache_file" ]; then
        local cached_key cached_answer
        # ``IFS=`` so a path carrying whitespace survives the read verbatim.
        { IFS= read -r cached_key; IFS= read -r cached_answer; } <"$cache_file" || true
        if [ "${cached_key:-}" = "$key" ] && [ -n "${cached_answer:-}" ] && [ -x "${cached_answer}" ]; then
            printf '%s' "$cached_answer"
            return 0
        fi
    fi

    local path
    for path in "${candidates[@]:-}"; do
        [ -n "$path" ] || continue
        if _uv_answers "$path"; then
            _uv_write_cache "$cache_file" "$key" "$path"
            printf '%s' "$path"
            return 0
        fi
    done
    return 1
}

_uv_env_is_foreign() {
    # True when *cfg* records an interpreter this machine does not have — the
    # bind-mounted host environment as seen from inside the container.
    local home
    home="$(sed -n 's/^[[:space:]]*home[[:space:]]*=[[:space:]]*//p' "$1" 2>/dev/null | head -n1)"
    [ -n "$home" ] && [ ! -d "$home" ]
}

_uv_project_is_workspace_member() {
    # True when an ancestor of *project* declares a uv workspace — which makes
    # *project* a MEMBER, and makes the environment `uv run --project` reconciles
    # the ROOT's, shared with every other project in the workspace.
    local dir i
    dir="$(dirname "$1")"
    for ((i = 0; i < _UV_ENV_SEARCH_DEPTH; i++)); do
        if [ -f "${dir}/pyproject.toml" ] &&
            grep -q '^[[:space:]]*\[tool\.uv\.workspace\]' "${dir}/pyproject.toml" 2>/dev/null; then
            return 0
        fi
        [ "$dir" = "/" ] && break
        dir="$(dirname "$dir")"
    done
    return 1
}

uv_project_run_prefix() {
    # Set UV_PROJECT_RUN: the command prefix that runs *uv* against *project*
    # without letting it reconcile an environment that is not ours.
    #
    # `uv run --project <dir>` SYNCS before running, and for a workspace MEMBER the
    # environment it reconciles is the workspace ROOT's — so a hook in a vendored
    # `vendor/teatree` reaches the fork root's `.venv`. Two distinct ways that
    # harms the caller, and the second is the one that looks like someone else's
    # bug:
    #
    #   * uv REMOVES an environment whose interpreter it cannot use, which is how a
    #     containerized hook deleted the operator's live host environment on every
    #     commit;
    #   * and even when the interpreter IS usable, uv reconciles that shared
    #     environment to the MEMBER's dependency set, uninstalling everything only
    #     the root declares. Nothing errors. The next command in the same push —
    #     a hook nobody associates with this one — dies on ModuleNotFoundError
    #     naming a package no diff went near.
    #
    # The second harm does not need a foreign interpreter, so membership alone
    # decides: every workspace member redirects, and every NON-member is left
    # untouched — a standalone project that owns its environment keeps being
    # managed in place, and its cold clone still builds its own.
    #
    # Deliberately NOT memoised alongside the binary: the boundary this inspects
    # can change while every candidate stays byte-identical.
    local uv_path="$1" project="$2"
    UV_PROJECT_RUN=("$uv_path")

    if _uv_project_is_workspace_member "$project"; then
        UV_PROJECT_RUN=(env "UV_PROJECT_ENVIRONMENT=${_UV_HOOK_ENV_NAME}" "$uv_path")
        return 0
    fi

    local dir="$project" cfg i
    for ((i = 0; i < _UV_ENV_SEARCH_DEPTH; i++)); do
        cfg="${dir}/.venv/pyvenv.cfg"
        if [ -f "$cfg" ]; then
            if _uv_env_is_foreign "$cfg"; then
                UV_PROJECT_RUN=(env "UV_PROJECT_ENVIRONMENT=${_UV_HOOK_ENV_NAME}" "$uv_path")
            fi
            return 0
        fi
        [ "$dir" = "/" ] && break
        dir="$(dirname "$dir")"
    done
    return 0
}
