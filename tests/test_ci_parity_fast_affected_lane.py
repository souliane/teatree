"""``dev/ci-parity-fast.sh`` runs the AFFECTED-tests lane, not a broad directory (#3587).

The inner loop used to run ``tests/quality -m "not push_heavy"`` every iteration
(~420s even with the heavy classes deselected). This pins that the fast lane now
delegates to ``dev/test-affected.sh`` — the diff-scoped selector that degrades to
the whole suite only on an unclassifiable change — and no longer runs the broad
``tests/quality`` directory directly. The whole-tree coverage floor stays CI's
sharded ``test (3.13)`` lane's job (``tests/test_no_full_suite_on_pre_push.py``).
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAST = _REPO_ROOT / "dev" / "ci-parity-fast.sh"


def _run_lines() -> list[str]:
    return [line for line in _FAST.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]


class TestCiParityFastUsesAffectedLane:
    def test_script_exists_and_is_executable(self) -> None:
        assert _FAST.is_file(), "dev/ci-parity-fast.sh must exist"

    def test_invokes_the_affected_tests_lane(self) -> None:
        assert any("dev/test-affected.sh" in line for line in _run_lines()), (
            "the fast inner loop must delegate its test step to dev/test-affected.sh (the diff-scoped selector)."
        )

    def test_does_not_run_the_broad_quality_dir_directly(self) -> None:
        offenders = [line for line in _run_lines() if "tests/quality" in line]
        assert not offenders, (
            "dev/ci-parity-fast.sh must NOT run the broad `tests/quality` directory directly -- the "
            f"affected-tests lane already carries it as a floor dir on a scoped selection: {offenders}"
        )

    def test_keeps_migration_graph_and_push_gate_checks(self) -> None:
        body = _FAST.read_text()
        assert "makemigrations --check" in body, "the migration-graph linearity check must stay in the fast lane."
        assert "t3 tool push-gate --run" in body, "the incremental push gate must stay in the fast lane."
