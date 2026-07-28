#!/usr/bin/env bash

# Select a Python >= 3.11 interpreter to run a teatree hook script — preferring
# one that can import Django.
#
# The hook modules use 3.11+ stdlib (`tomllib`, imported at module level in
# `teatree_settings.py`) and modern typing; the project baseline is >=3.13.
# Some hosts resolve a bare `python3` to an older runtime (e.g. macOS system
# Python 3.9), where `hook_router.py` crashes at import — taking down EVERY
# hooked session at bootstrap. This shim picks an available >= 3.11 interpreter
# and execs it with the forwarded arguments (the hook script path plus its
# flags), so `hooks.json` never depends on what bare `python3` happens to be on
# a given host.
#
# The version floor alone is not enough. `django_bootstrap.bootstrap_teatree_django`
# puts the sibling `src/` on `sys.path` and calls `django.setup()`, but Django
# itself must come from the INTERPRETER — and teatree is installed into a uv-tool
# venv, not into the system python a bare `python3` resolves to. On such a host
# the bootstrap returns False and EVERY DB-backed handler silently no-ops: the
# SessionStart hand-off drain, the away-mode `DeferredQuestion` recorder, pending
# chat injections, the loop-owner/registration readers, and the standing-goal /
# orchestrator-investigation / unknown-repo-push gates. Nothing errors — the work
# just never happens, which is how hand-offs accumulated unclaimed for a week.
#
# This is the ORM-tier sibling of #3499: that fix taught the Django-free cold
# reader to bootstrap `src/` so kill-switch flags stopped resolving to their
# compiled-in defaults. A cold sqlite read can be repaired in-process; an ORM
# import cannot, because a running interpreter without Django installed can never
# acquire it. So the choice has to move up here, to interpreter selection.
#
# Pass 1 therefore requires `import django` as well as the version floor, and
# considers the interpreter teatree is installed into first — discovered next to
# the resolved `t3` entry point (a uv-tool / venv layout puts `python` beside it),
# so nothing is hard-coded to one host. `T3_HOOK_PYTHON` short-circuits the whole
# search on the version floor alone — an operator naming an interpreter outranks
# the Django preference.
#
# Fail open: pass 2 repeats the search with the version floor ALONE, so a host
# with no Django-capable interpreter keeps exactly the pre-existing behaviour —
# the file-mirror hand-off fallback and every Django-free gate still run. If no
# >= 3.11 interpreter is found at all, exit 0 silently so a hook is a no-op
# rather than a session-breaking crash — the same crash-proof / silent contract
# every hook honours (hooks/CLAUDE.md). A broken interpreter shim (e.g. a pyenv
# shim for an uninstalled version) fails its probe and is skipped.

set -u

# Does $1 clear the version floor, and — when $2 is "django" — import Django?
probe_interpreter() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 || return 1
    [ "${2:-}" = "django" ] || return 0
    "$1" -c 'import django' >/dev/null 2>&1
}

# Interpreter candidates, most-preferred first, one per line. Paths may not
# exist; the probe filters them.
interpreter_candidates() {
    # The venv teatree itself is installed into, found beside the `t3` entry
    # point. `command -v` is a shell builtin and `${var%/*}` is expansion, so
    # this needs no external binary — a hook subprocess inherits a restricted
    # PATH, and shelling out to `dirname` there silently yields no candidate.
    # `readlink` IS external, so it is strictly best-effort: `~/.local/bin/t3` is
    # typically a symlink INTO the venv, so try the resolved dir first, but keep
    # the unresolved dir as a candidate for when `readlink` is unavailable or
    # non-GNU (BSD/macOS lack `-f`).
    t3_bin="$(command -v t3 2>/dev/null || true)"
    if [ -n "$t3_bin" ]; then
        t3_resolved="$(readlink -f "$t3_bin" 2>/dev/null || true)"
        for t3_path in "$t3_resolved" "$t3_bin"; do
            case "$t3_path" in
                */*) printf '%s\n' "${t3_path%/*}/python" "${t3_path%/*}/python3" ;;
            esac
        done
    fi

    # The default uv-tool layout, so the venv is still found when `readlink` is
    # unavailable and `t3` is a symlink from elsewhere.
    if [ -n "${HOME:-}" ]; then
        printf '%s\n' "$HOME/.local/share/uv/tools/teatree/bin/python"
    fi

    for name in python3.13 python3.12 python3.11 python3; do
        command -v "$name" 2>/dev/null || true
    done
}

# An operator naming an interpreter outranks the search: honour it on the
# version floor alone, so `T3_HOOK_PYTHON` stays a true override (and a usable
# escape hatch when the Django-capable pick is the one misbehaving).
if [ -n "${T3_HOOK_PYTHON:-}" ] && [ -x "${T3_HOOK_PYTHON}" ] && probe_interpreter "$T3_HOOK_PYTHON"; then
    exec "$T3_HOOK_PYTHON" "$@"
fi

# Split on newline only, so an interpreter path containing spaces survives.
old_ifs="$IFS"
IFS='
'
for requirement in django ""; do
    for bin in $(interpreter_candidates); do
        [ -n "$bin" ] || continue
        [ -x "$bin" ] || continue
        if probe_interpreter "$bin" "$requirement"; then
            IFS="$old_ifs"
            exec "$bin" "$@"
        fi
    done
done
IFS="$old_ifs"

exit 0
