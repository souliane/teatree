#!/usr/bin/env bash
# Run tests in Docker across all supported Python versions (mirrors CI).
# Uses a cached image with apt packages + uv pre-installed.
# Caches uv data (Python installs) and venvs between runs to avoid re-downloading.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="teatree-test"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/teatree-test"
mkdir -p "${CACHE_DIR}/uv"
failed=0
versions=(3.13)
total=${#versions[@]}
current=0

# Build (or reuse cached) test image
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "=== Building test image (first run only) ==="
    docker build -q -t "$IMAGE" -f dev/Dockerfile.test . >/dev/null
    echo
fi

for py in "${versions[@]}"; do
    current=$((current + 1))
    echo "=== [$current/$total] Python $py ==="
    # Capture output to a temp file — piping 1600+ test lines through
    # Docker → git pre-push hook overflows the stderr buffer (Rust panic).
    tmpout=$(mktemp)
    venv_dir="${CACHE_DIR}/venv-${py}"
    mkdir -p "$venv_dir"
    if docker run --rm \
        -v "$PWD":/app:ro \
        -v "${venv_dir}":/tmp/.venv \
        -v "${CACHE_DIR}/uv":/tmp/.uv \
        -e UV_PROJECT_ENVIRONMENT=/tmp/.venv \
        -e UV_CACHE_DIR=/tmp/.uv/cache \
        -e UV_PYTHON_INSTALL_DIR=/tmp/.uv/python \
        -e COVERAGE_FILE=/tmp/.coverage \
        "$IMAGE" \
        uv run -p "$py" pytest --no-header --no-cov -q --tb=short \
            -o cache_dir=/tmp/.pytest_cache > "$tmpout" 2>&1; then
        # Show only the summary line (last non-empty line)
        tail -3 "$tmpout"
        echo "  -> Python $py: OK"
    else
        # On failure, show last 30 lines for diagnosis
        tail -30 "$tmpout"
        echo "  -> Python $py: FAILED"
        failed=1
    fi
    rm -f "$tmpout"
    echo
done

if [ "$failed" -eq 0 ]; then
    echo "=== All $total versions passed ==="
else
    echo "=== Some versions failed ==="
fi

exit $failed
