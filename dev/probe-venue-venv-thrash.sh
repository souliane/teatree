#!/usr/bin/env bash
# Does a host/container venue flip destroy a worktree's virtualenv? (#4642)
#
# A `.venv/pyvenv.cfg` records its interpreter's ABSOLUTE path. While the two
# venues installed interpreters into different roots, each judged the other's
# environment invalid and deleted it — ~1 GB a flip, silently. This builds a
# throwaway environment in one venue, drops a sentinel inside it, runs `uv` from
# the other, and reports whether the sentinel survived.
#
# Exits non-zero on any destroyed environment, so it is usable as the
# restart-and-verify acceptance check (deploy/README.md § interpreter plane).
set -euo pipefail

ALTERNATIONS=1
while [ $# -gt 0 ]; do
    case "$1" in
    --alternations)
        ALTERNATIONS="$2"
        shift 2
        ;;
    -h | --help)
        sed -n '2,11p' "$0"
        echo
        echo "usage: $(basename "$0") [--alternations N]"
        exit 0
        ;;
    *)
        echo "unknown argument: $1" >&2
        exit 2
        ;;
    esac
done

COMPOSE_FILE="${TEATREE_COMPOSE_FILE:-${TEATREE_DEPLOY_CHECKOUT:-$HOME/teatree-deploy}/deploy/docker-compose.yml}"
[ -f "$COMPOSE_FILE" ] || {
    echo "probe: no compose file at $COMPOSE_FILE — set TEATREE_COMPOSE_FILE" >&2
    exit 2
}

# The probe must live INSIDE the path-identically shared tree: that sharing is
# the precondition for the failure, so a probe outside it can never reproduce.
WORKSPACES="${TEATREE_HOST_HOME:-$HOME}/workspace/t3-workspaces"
PROBE_DIR="$WORKSPACES/_probe-venue-venv-thrash-$$"
trap 'rm -rf "$PROBE_DIR"' EXIT

mkdir -p "$PROBE_DIR"
cat >"$PROBE_DIR/pyproject.toml" <<'TOML'
[project]
name = "venue-probe"
version = "0"
requires-python = ">=3.13"
dependencies = []
TOML

in_container() {
    docker compose -f "$COMPOSE_FILE" exec -T -w "$PROBE_DIR" teatree-worker sh -c "$1"
}

on_host() {
    (cd "$PROBE_DIR" && sh -c "$1")
}

failures=0
for i in $(seq 1 "$ALTERNATIONS"); do
    for direction in host-to-container container-to-host; do
        rm -rf "$PROBE_DIR/.venv"
        case "$direction" in
        host-to-container)
            on_host 'uv venv --quiet'
            builder=on_host
            reader=in_container
            ;;
        container-to-host)
            in_container 'uv venv --quiet'
            builder=in_container
            reader=on_host
            ;;
        esac
        "$builder" 'true'
        sentinel="$PROBE_DIR/.venv/VENUE_PROBE_SENTINEL"
        : >"$sentinel"
        recorded="$(grep '^home' "$PROBE_DIR/.venv/pyvenv.cfg" | cut -d= -f2- | tr -d ' ')"

        "$reader" 'uv run --no-project python -c "pass"' >/dev/null 2>&1 || true

        if [ -e "$sentinel" ]; then
            echo "PASS  round $i $direction: environment intact (built against $recorded)"
        else
            echo "FAIL  round $i $direction: the reading venue DESTROYED the environment built against $recorded"
            failures=$((failures + 1))
        fi
        echo "      .venv now $(du -sh "$PROBE_DIR/.venv" 2>/dev/null | cut -f1)"
    done
done

[ "$failures" -eq 0 ] || {
    echo "probe: $failures venue flip(s) destroyed the environment — the interpreter roots still differ" >&2
    exit 1
}
echo "probe: $((ALTERNATIONS * 2)) venue flips, no rebuild"
