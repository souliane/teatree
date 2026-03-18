#!/usr/bin/env bash
# Run tests across all supported Python versions in Docker (parallel).
set -euo pipefail

command -v parallel &>/dev/null || brew install parallel

run_one() {
    docker run --rm -v "$PWD":/app -w /app "python:$1-slim" \
        sh -c "apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 && pip install -q uv && uv run pytest --no-header -q"
}
export -f run_one
export PWD

parallel --tag --line-buffer run_one ::: 3.12 3.13 3.14
