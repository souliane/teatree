#!/usr/bin/env bash
# Run tests in Docker across all supported Python versions (mirrors CI).
# Uses ubuntu:latest + uv, same as GitHub Actions ubuntu-latest runner.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="ubuntu:latest"
failed=0

for py in 3.12 3.13 3.14; do
    echo "=== Python $py ==="
    if docker run --rm -v "$PWD":/app -w /app "$IMAGE" \
        bash -c "
            set -e
            apt-get update -qq >/dev/null
            apt-get install -y -qq curl git >/dev/null 2>&1
            curl -LsSf https://astral.sh/uv/install.sh | sh -s -- -q 2>/dev/null
            export PATH=\$HOME/.local/bin:\$PATH
            uv run -p $py pytest --no-header -q
        "; then
        echo "--- Python $py: OK ---"
    else
        echo "--- Python $py: FAILED ---"
        failed=1
    fi
    echo
done

exit $failed
