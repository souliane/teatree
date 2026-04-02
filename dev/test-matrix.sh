#!/usr/bin/env bash
# Run tests in Docker across all supported Python versions (mirrors CI).
# Uses a cached image with apt packages + uv pre-installed.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="teatree-test"
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
    if docker run --rm \
        -v "$PWD":/app:ro \
        -e UV_PROJECT_ENVIRONMENT=/tmp/.venv \
        -e COVERAGE_FILE=/tmp/.coverage \
        "$IMAGE" \
        uv run -p "$py" pytest --no-header -v \
            -o cache_dir=/tmp/.pytest_cache; then
        echo "  -> Python $py: OK"
    else
        echo "  -> Python $py: FAILED"
        failed=1
    fi
    echo
done

if [ "$failed" -eq 0 ]; then
    echo "=== All $total versions passed ==="
else
    echo "=== Some versions failed ==="
fi

exit $failed
