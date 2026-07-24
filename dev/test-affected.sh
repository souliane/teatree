#!/usr/bin/env bash
# Opt-in fast-feedback lane: run only the tests a diff affects (#113, #3672).
#
# The impact engine is the tach pytest plugin (`--tach --tach-base origin/main`): it
# walks the reverse-import graph natively and deselects the tests a diff cannot reach.
# `t3 tool affected-tests` decides FULL-vs-scoped from the ESCALATION policy and emits
# the pytest invocation: a scoped run activates the plugin AND loads our force-keep
# layer (`-p teatree.quality.force_keep_plugin`), which keeps the floor dirs, the
# doc-reader tests, the mirror paths, and the changed test files over the plugin's
# deselection — in ONE session, so zero test runs twice.
#
# ANY unclassifiable EXECUTABLE change (conftest/settings/migrations/data files/
# deletions/files outside the modelled roots) degrades to the WHOLE suite with the
# plugin OFF. Under-run is a false green, so the escalation stays. Over-run is not free
# either (#3645): a measured escalation ran 30182 tests in 59m32s for a one-module fix.
# Docs (markdown / the docs tree / mkdocs config) are therefore classified as having no
# executable semantics and force-keep only the tests that READ them, rather than the tree.
#
# NOT a gate. The 12-shard CI run + 93% combined-coverage floor stays the merge gate,
# and pre-push is untouched (`tests/test_no_full_suite_on_pre_push.py`). Use this while
# iterating; run `bash dev/test-fast.sh` (or `bash dev/ci-parity.sh`) before pushing.
#
# Usage:
#   bash dev/test-affected.sh                 # select + run against origin/main
#   bash dev/test-affected.sh --base <ref>    # select against a different merge-base
#   bash dev/test-affected.sh --full          # skip selection, run the whole suite
#   bash dev/test-affected.sh -- <pytest arg> # forward extra args to pytest
set -euo pipefail
cd "$(dirname "$0")/.."

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
