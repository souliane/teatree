#!/usr/bin/env bash
# Opt-in fast-feedback lane: run only the tests a diff affects (#113).
#
# Safety-biased selection (`t3 tool affected-tests`): a changed src module expands to
# its transitive reverse-import dependents + the tests importing any of them + the
# mirror path + an always-run floor. ANY unclassifiable EXECUTABLE change (conftest/
# settings/migrations/data files/deletions/files outside the modelled roots) degrades
# to the WHOLE suite.
#
# Under-run is a false green, so the escalation stays. Over-run is not free either
# (#3645): a measured escalation ran 30182 tests in 59m32s for a one-module fix and
# manufactured 30 shared-box artifact failures that then had to be disproved. Docs
# (markdown / the docs tree / mkdocs config) are therefore classified as having no
# executable semantics and select only the tests that READ them, rather than the tree.
#
# The report step also prints the #3672 ADVISORY `selector-comparison` line: the tach
# pytest plugin's impact verdict, computed report-only and NEVER applied, diffed against
# our selection. `theirs_only` (tests the plugin keeps and we drop) is the only direction
# that could ever produce a false green, so it is counted separately. Collect that
# divergence over real diffs — it is the gate for a later cutover, and changes nothing
# today. The `--pytest-args` path does not compute it, so the hot path is unaffected.
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

# The selector emits the one-line over-run report to the human report; --pytest-args
# emits exactly the positional args (test files, --doctest-modules targets, floor dirs,
# and --create-db when a migration forced FULL+create-db).
t3 tool affected-tests --base "$BASE"
echo "==="

read -r -a SELECTED <<< "$(t3 tool affected-tests --base "$BASE" --pytest-args)"
# No positional args ⇒ a FULL verdict (empty selection) ⇒ run the whole suite.
exec uv run pytest --no-cov -n auto --reuse-db "${SELECTED[@]}" "${PYTEST_EXTRA[@]}"
