#!/usr/bin/env bash

# Select a Python >= 3.11 interpreter to run a teatree hook script.
#
# The hook modules use 3.11+ stdlib (`tomllib`, imported at module level in
# `teatree_settings.py`) and modern typing; the project baseline is >=3.13.
# Some hosts resolve a bare `python3` to an older runtime (e.g. macOS system
# Python 3.9), where `hook_router.py` crashes at import — taking down EVERY
# hooked session at bootstrap. This shim picks the newest available >= 3.11
# interpreter and execs it with the forwarded arguments (the hook script path
# plus its flags), so `hooks.json` never depends on what bare `python3` happens
# to be on a given host.
#
# Fail open: if no >= 3.11 interpreter is found, exit 0 silently so a hook is a
# no-op rather than a session-breaking crash — the same crash-proof / silent
# contract every hook honours (hooks/CLAUDE.md). A broken interpreter shim
# (e.g. a pyenv shim for an uninstalled version) fails its probe and is skipped.

set -u

for candidate in python3.13 python3.12 python3.11 python3; do
    bin="$(command -v "$candidate" 2>/dev/null)" || continue
    [ -n "$bin" ] || continue
    if "$bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        exec "$bin" "$@"
    fi
done

exit 0
