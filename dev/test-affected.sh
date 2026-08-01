#!/usr/bin/env bash
# The LOCAL DEFAULT lane: run only the tests a diff affects (#113, #3672, #3994).
#
# The impact engine is the tach pytest plugin (`--tach --tach-base origin/main`): it
# walks the reverse-import graph natively and deselects the tests a diff cannot reach.
# `t3 tool affected-tests` decides FULL-vs-scoped from the ESCALATION policy and emits
# the pytest invocation: a scoped run activates the plugin AND loads our force-keep
# layer (`-p teatree.quality.force_keep_plugin`), which keeps the floor dirs, the
# reference-reader tests, the mirror paths, and the changed test files over the plugin's
# deselection — in ONE session, so zero test runs twice.
#
# ANY unclassifiable EXECUTABLE change (conftest/settings/migrations/data files/
# deletions/files outside the modelled roots) degrades to the WHOLE suite with the
# plugin OFF. Under-run is a false green, so the escalation stays. Over-run is not free
# either (#3645): a measured escalation ran 30182 tests in 59m32s for a one-module fix.
# Paths NOTHING imports — docs (markdown / the docs tree / mkdocs config) and the `dev/`
# lane runners (#3817) — are therefore classified as having no executable semantics and
# force-keep only the tests whose source NAMES them, rather than the whole tree.
#
# This file is one of `SELECTION_DEFINING_PATHS`: editing it (or the quality modules the
# selector is built from) forces FULL, because a selection cannot validate a change to
# the code that computes it. A whole-suite run is ~34k tests; on a memory-tight host
# bound the parallelism rather than abandoning the run — pytest-xdist resolves `-n auto`
# through PYTEST_XDIST_AUTO_NUM_WORKERS, so `PYTEST_XDIST_AUTO_NUM_WORKERS=2 bash
# dev/test-affected.sh` trades wall-clock for a run that finishes instead of being OOM-
# killed. CI's sharded `test (3.13)` lane remains the authority either way.
#
# NOT a gate. The 12-shard CI run + 93% combined-coverage floor stays the merge gate,
# and pre-push is untouched (`tests/test_no_full_suite_on_pre_push.py`). Use this while
# iterating; run `bash dev/ci-parity-fast.sh` before pushing (it calls this lane). The
# whole-suite runners (`--full`, `dev/test-fast.sh`, `dev/ci-parity.sh`) are the DECLARED
# exception for a genuinely cross-cutting change, not the per-ticket default (#3994).
#
# Usage:
#   bash dev/test-affected.sh                 # select + run against origin/main
#   bash dev/test-affected.sh --base <ref>    # select against a different merge-base
#   bash dev/test-affected.sh --full          # skip selection, run the whole suite
#   bash dev/test-affected.sh -- <pytest arg> # forward extra args to pytest
#   PYTEST_XDIST_AUTO_NUM_WORKERS=2 bash dev/test-affected.sh   # bound a FULL run's RAM
set -euo pipefail
cd "$(dirname "$0")/.."

# `-n auto` sizes the worker pool from CPU count, which a cgroup memory cap does not
# change — so a memory-capped container spawns host-many workers and dies as an opaque
# xdist crash. Default the pool from the cap instead (an explicit
# PYTEST_XDIST_AUTO_NUM_WORKERS still wins; an uncapped box is left alone).
. dev/lib/xdist-workers.sh
bound_xdist_workers_to_memory

BASE="origin/main"
FULL=0
PYTEST_EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE="$2"; shift 2 ;;
        --full) FULL=1; shift ;;
        --) shift; PYTEST_EXTRA+=("$@"); break ;;
        *) PYTEST_EXTRA+=("$1"); shift ;;
    esac
done

if [[ "$FULL" == "1" ]]; then
    echo "=== affected-tests: --full — running the whole suite ==="
    exec uv run pytest --no-cov -n auto --reuse-db "${PYTEST_EXTRA[@]}"
fi

# The selector prints the FULL-vs-scoped report; --pytest-args emits the invocation:
# a scoped run adds `--tach --tach-base <base> -p teatree.quality.force_keep_plugin`
# (plugin deselects, force-keep layer re-adds our escalations) plus any --doctest-modules
# targets and --create-db; a FULL run emits at most --create-db and runs the whole suite.
t3 tool affected-tests --base "$BASE"
echo "==="

read -r -a SELECTED <<< "$(t3 tool affected-tests --base "$BASE" --pytest-args)"
# A FULL verdict emits no --tach flag ⇒ the whole suite runs.
exec uv run pytest --no-cov -n auto --reuse-db "${SELECTED[@]}" "${PYTEST_EXTRA[@]}"
