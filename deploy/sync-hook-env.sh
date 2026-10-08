#!/usr/bin/env bash
# Re-sync the host hook tool env (run-hook.sh's interpreter) to the lock of the checkout at $1, then verify it.
set -euo pipefail

REPO_REAL="$(cd "${1:?usage: sync-hook-env.sh <repo-root>}" && pwd -P)"
HOOK_ENV="${UV_TOOL_DIR:-$HOME/.local/share/uv/tools}/teatree"

if [ ! -x "$HOOK_ENV/bin/python" ]; then
    echo "deploy: no host hook tool env at $HOOK_ENV; nothing to re-sync."
    exit 0
fi

owner="$(sed -n '/name = "teatree"/s/.*editable = "\([^"]*\)".*/\1/p' "$HOOK_ENV/uv-receipt.toml" 2>/dev/null)" || owner=""
owner_real=""
if [ -n "$owner" ]; then
    owner_real="$(cd "$owner" 2>/dev/null && pwd -P)" || owner_real=""
fi
if [ "$owner_real" != "$REPO_REAL" ]; then
    echo "deploy: the host hook tool env $HOOK_ENV is installed from ${owner:-an unrecorded source}, not $REPO_REAL; leaving it alone."
    exit 0
fi

. "$REPO_REAL/scripts/hooks/lib/resolve-uv.sh"
uv_rc=0
UV="$(resolve_uv)" || uv_rc=$?
if [ "$uv_rc" -ne 0 ]; then
    echo "deploy: FATAL — no working uv for the host hook tool env re-sync (resolve_uv rc $uv_rc); install uv at ~/.local/bin/uv." >&2
    exit 1
fi

# --python reuses the env's interpreter so uv never recreates the dir and drops uv-receipt.toml.
FIX="UV_PROJECT_ENVIRONMENT=$HOOK_ENV uv sync --project $REPO_REAL --frozen --no-default-groups --inexact --python $HOOK_ENV/bin/python"
echo "deploy: re-syncing the host hook tool env $HOOK_ENV to $REPO_REAL ..."
if ! UV_PROJECT_ENVIRONMENT="$HOOK_ENV" "$UV" sync --project "$REPO_REAL" --frozen --no-default-groups --inexact --python "$HOOK_ENV/bin/python"; then
    echo "deploy: FATAL — could not re-sync the host hook tool env $HOOK_ENV; fix: $FIX" >&2
    exit 1
fi

verify_rc=0
stale="$(PYTHONPATH="$REPO_REAL/src" "$HOOK_ENV/bin/python" -m teatree.utils.dep_skew "$REPO_REAL/pyproject.toml")" || verify_rc=$?
if [ "$verify_rc" -eq 0 ]; then
    echo "deploy: host hook tool env $HOOK_ENV re-synced to $REPO_REAL."
    exit 0
fi
if [ "$verify_rc" -eq 1 ] && [ -n "$stale" ]; then
    echo "deploy: FATAL — host hook tool env $HOOK_ENV still stale: ${stale//$'\n'/; } — fix: $FIX" >&2
    exit 1
fi
echo "deploy: FATAL — could not verify the host hook tool env $HOOK_ENV (rc $verify_rc) — fix: $FIX" >&2
exit 1
