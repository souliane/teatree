#!/usr/bin/env bash
# Combined unit + e2e coverage report.
#
# Runs the unit suite and the Playwright e2e suite **in parallel** (they're
# fully isolated: tests use an in-memory SQLite, e2e uses its own per-worker
# DB), captures coverage from both processes (pytest + the uvicorn subprocess
# Playwright drives — auto-instrumented via `patch=["subprocess"]` in
# pyproject.toml), combines the data files, and writes a terminal report
# plus an HTML one to htmlcov/.
#
# Each pytest invocation gets its own COVERAGE_FILE so the parallel writes
# don't race. `coverage combine` then merges every .coverage.* file (including
# the per-PID files the patched subprocess leaves behind) into a single
# .coverage that `report` and `html` consume.
#
# Local: ./scripts/coverage.sh
# CI: the workflow uploads each job's .coverage.* artifacts and combines them
#     in a small follow-up job — no test re-run.
set -euo pipefail

uv run coverage erase

# --cov-fail-under=0 disables the per-run threshold — the 93% gate is enforced
# on the combined report below.
COVERAGE_FILE=.coverage.unit uv run pytest tests/ --cov --cov-report= --cov-fail-under=0 &
unit_pid=$!
# Match CI: `t3 teatree e2e project --no-docker` is the same entry point both
# local and CI use. The e2e management command appends --cov when COVERAGE_FILE
# is set (see src/teatree/core/management/commands/e2e.py).
COVERAGE_FILE=.coverage.e2e t3 teatree e2e project --no-docker &
e2e_pid=$!

unit_rc=0
e2e_rc=0
wait "$unit_pid" || unit_rc=$?
wait "$e2e_pid" || e2e_rc=$?

if [[ $unit_rc -ne 0 || $e2e_rc -ne 0 ]]; then
  echo "Test failure (unit=$unit_rc, e2e=$e2e_rc)" >&2
  exit 1
fi

uv run coverage combine
uv run coverage report --fail-under=93
uv run coverage html
echo "HTML report: htmlcov/index.html"
