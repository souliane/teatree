#!/usr/bin/env bash
# Opt-in local suite on Python 3.13 (CI's version), host + parallel.
# Not a push gate: push -> CI runs the suite (#112/#21/#38). The opt-in Docker
# superset is dev/test-matrix.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION="${TEATREE_TEST_PYTHON:-3.13}"

echo "=== Fast pre-push: Python ${PY_VERSION} (host, parallel) ==="
# Output is captured: streaming 11k+ lines through the git hook panics its buffer.
# Coverage floor is CI's job (`test`); --no-cov keeps the gate fast.
tmpout=$(mktemp)
if uv run -p "${PY_VERSION}" pytest \
    --no-header --no-cov -q --tb=short \
    -p no:cacheprovider \
    -n auto --dist worksteal \
    -o "addopts=--color=yes --doctest-modules --strict-config --strict-markers --reuse-db" \
    > "$tmpout" 2>&1; then
    tail -4 "$tmpout"
    echo "  -> Python ${PY_VERSION}: OK"
    rm -f "$tmpout"
    exit 0
else
    tail -40 "$tmpout"
    echo "  -> Python ${PY_VERSION}: FAILED"
    rm -f "$tmpout"
    exit 1
fi
