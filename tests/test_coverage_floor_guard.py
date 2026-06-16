"""Coverage-config guardrail.

If you're reading this because a test failed, an agent (or human) tried to
lower the project's coverage floor or otherwise bypass measurement. **Do not
edit this file to make the test pass.** Either:

1. Restore the coverage config (raise ``fail_under`` back, drop the omit), or
2. Get explicit human approval and update the constants below in the same
    change so the loosening is visible in code review.

The floor exists because CI on ``main`` had drifted under 93% across five
commits before anyone noticed — see PR #623 for the cleanup. Without a
codified floor, the same drift would happen again. New uncovered code must
ship with tests.
"""

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_COV_LANE_SCRIPT = _REPO_ROOT / "dev" / "test-cov.sh"

# The agreed-on coverage floor. Decreases require explicit human approval and
# an update to this constant in the same PR.
MIN_FAIL_UNDER = 93

# Coverage measurement boundaries. Adding entries hides code from coverage —
# any growth needs an explicit reason in the PR description.
ALLOWED_OMIT_PATTERNS: frozenset[str] = frozenset(
    {
        # Django migrations are auto-generated and not meaningfully testable.
        "src/teatree/core/migrations/*.py",
    },
)
ALLOWED_SOURCE_PATHS: frozenset[str] = frozenset({"src/teatree"})

# The default pytest addopts is lean and parallel (no coverage) so the inner
# loop stays fast; coverage is enforced in the dedicated CI ``test`` lane and
# ``dev/test-cov.sh`` (see CONTRIBUTING / tests/README.md). The guard below
# locks that enforcement point. Adding ``--no-cov`` / ``--cov-fail-under=0`` to
# the default addopts would silently disarm even an ad-hoc coverage run, so it
# stays banned from the default.
BANNED_PYTEST_FLAGS: frozenset[str] = frozenset({"--no-cov", "--cov-fail-under=0"})

# Flags the coverage-enforcing invocations (CI heavy lane + ``dev/test-cov.sh``)
# MUST carry. Dropping any of these silently weakens the gate: ``--cov`` /
# ``--cov-branch`` stop measuring, ``--doctest-modules`` drops doctest coverage
# (which contributes to the floor), ``--cov-fail-under=93`` stops failing below
# the floor.
REQUIRED_COVERAGE_LANE_FLAGS: frozenset[str] = frozenset(
    {"--cov", "--cov-branch", "--doctest-modules", "--cov-fail-under=93"},
)


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


class TestCoverageFloor:
    def test_fail_under_meets_minimum(self, pyproject: dict) -> None:
        fail_under = pyproject["tool"]["coverage"]["report"]["fail_under"]
        assert fail_under >= MIN_FAIL_UNDER, (
            f"Coverage floor was lowered to {fail_under}. "
            f"This file's MIN_FAIL_UNDER expects >= {MIN_FAIL_UNDER}. "
            f"If you're intentionally lowering it, update MIN_FAIL_UNDER too."
        )

    def test_coverage_source_paths_locked(self, pyproject: dict) -> None:
        source = set(pyproject["tool"]["coverage"]["run"].get("source", []))
        unexpected = source - ALLOWED_SOURCE_PATHS
        missing = ALLOWED_SOURCE_PATHS - source
        assert not unexpected, f"Unexpected coverage source paths added: {unexpected}"
        assert not missing, f"Coverage source paths removed: {missing}"

    def test_coverage_omit_list_locked(self, pyproject: dict) -> None:
        omit = set(pyproject["tool"]["coverage"]["run"].get("omit", []))
        unexpected = omit - ALLOWED_OMIT_PATTERNS
        assert not unexpected, (
            f"New coverage omit patterns added: {unexpected}. "
            f"If a file is genuinely untestable, prefer ``# pragma: no cover`` on the "
            f"specific lines, NOT whole-file exclusion. If exclusion is necessary, "
            f"add the pattern to ALLOWED_OMIT_PATTERNS in this test with a justification."
        )

    def test_coverage_report_omit_list_locked(self, pyproject: dict) -> None:
        omit = set(pyproject["tool"]["coverage"]["report"].get("omit", []))
        unexpected = omit - ALLOWED_OMIT_PATTERNS
        assert not unexpected, f"New report-level omit patterns added: {unexpected}"


# A coverage ``exclude_lines`` pattern that matches a clause opener (``def`` /
# ``class``) excludes the WHOLE block body from measurement, not one line —
# ``def main\(`` silently hid every ``main()`` body. Excludes must be
# line-scoped pragmas, never clause-level.
_CLAUSE_LEVEL_EXCLUDE = re.compile(r"\bdef\b|\bclass\b")


class TestExcludeLinesAreNotClauseLevel:
    def test_def_main_exclude_removed(self, pyproject: dict) -> None:
        excludes = pyproject["tool"]["coverage"]["report"]["exclude_lines"]
        assert "def main\\(" not in excludes, (
            "``def main\\(`` excludes every main() body from coverage. Removed in "
            "favor of per-line ``# pragma: no cover`` on genuinely untestable "
            "entry points (E11)."
        )

    def test_no_clause_level_exclude_patterns(self, pyproject: dict) -> None:
        excludes = pyproject["tool"]["coverage"]["report"]["exclude_lines"]
        offenders = [e for e in excludes if _CLAUSE_LEVEL_EXCLUDE.search(e)]
        assert not offenders, (
            f"Coverage exclude_lines contains clause-level pattern(s) {offenders} that "
            f"hide whole function/class bodies. Use a per-line ``# pragma: no cover`` on "
            f"the specific untestable line instead."
        )


def _addopts_str(pyproject: dict) -> str:
    addopts = pyproject["tool"]["pytest"]["ini_options"].get("addopts", "")
    return " ".join(addopts) if isinstance(addopts, list) else addopts


def _ci_test_lane_command() -> str:
    """The folded ``run: >`` block of the CI heavy lane that invokes pytest.

    Each block spans several lines (folded YAML ``>``); the coverage gate is the
    one whose body contains both ``pytest`` and ``--cov-fail-under``.
    """
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    blocks = re.findall(r"- run: >\n((?:[ \t]+.*\n)+)", text)
    coverage_blocks = [block for block in blocks if "pytest" in block and "--cov-fail-under" in block]
    assert coverage_blocks, "No CI ``run`` block invokes pytest with --cov-fail-under — coverage gate missing."
    return " ".join(coverage_blocks[0].split())


class TestPytestConfigNotBypassed:
    def test_default_addopts_does_not_disable_coverage(self, pyproject: dict) -> None:
        addopts_str = _addopts_str(pyproject)
        for flag in BANNED_PYTEST_FLAGS:
            assert flag not in addopts_str, (
                f"Default pytest addopts contains {flag!r}, which silently disables "
                f"coverage measurement. Use ``uv run pytest --no-cov`` ad-hoc for fast "
                f"iteration; never bake it into the default."
            )

    def test_default_addopts_runs_in_parallel(self, pyproject: dict) -> None:
        assert "-n auto" in _addopts_str(pyproject), (
            "Default pytest addopts no longer engages pytest-xdist (``-n auto``). "
            "The fast parallel default is the point of the test-speed config; "
            "if you must serialize, pass ``-n0`` ad-hoc, never bake it in."
        )

    def test_ci_lane_enforces_coverage_gate(self) -> None:
        command = _ci_test_lane_command()
        for flag in REQUIRED_COVERAGE_LANE_FLAGS:
            assert flag in command, (
                f"The CI heavy lane lost {flag!r}. Coverage moved off the default "
                f"addopts INTO this lane (and dev/test-cov.sh); dropping a flag here "
                f"silently disarms the 93% floor that gates every merge."
            )

    def test_local_coverage_lane_mirrors_ci(self) -> None:
        script = _COV_LANE_SCRIPT.read_text(encoding="utf-8")
        for flag in REQUIRED_COVERAGE_LANE_FLAGS:
            assert flag in script, (
                f"dev/test-cov.sh lost {flag!r}; it must stay CI-parity so a developer "
                f"can reproduce the coverage gate locally."
            )


# Minimum acceptable per-module floor. Lowering this constant requires the
# same explicit-approval rationale as MIN_FAIL_UNDER above.
MIN_PER_MODULE_FLOOR = 80


class TestPerModuleFloorsConfig:
    """Assert ``[tool.teatree.coverage] per_module_floors`` is well-formed.

    These are tighter floors than the project-wide 93% gate for newly-added
    modules where a small file dropping to 30% would still leave the project
    above 93%. Actual percentages are enforced by ``t3 ci coverage`` (run
    after pytest); this test verifies the config itself stays sane.
    """

    def test_entries_have_required_keys(self, pyproject: dict) -> None:
        entries = pyproject["tool"]["teatree"]["coverage"]["per_module_floors"]
        assert entries, "Per-module floors removed — every newly-added module should declare one."
        for entry in entries:
            assert set(entry.keys()) == {"path", "floor"}, (
                f"Each per_module_floors entry must have exactly 'path' and 'floor' keys; got {entry}"
            )

    def test_paths_exist(self, pyproject: dict) -> None:
        entries = pyproject["tool"]["teatree"]["coverage"]["per_module_floors"]
        for entry in entries:
            module_path = _REPO_ROOT / entry["path"]
            assert module_path.exists(), (
                f"Per-module floor refers to a missing path: {entry['path']}. "
                f"Either restore the module or remove the floor entry."
            )

    def test_floors_meet_minimum(self, pyproject: dict) -> None:
        entries = pyproject["tool"]["teatree"]["coverage"]["per_module_floors"]
        for entry in entries:
            assert entry["floor"] >= MIN_PER_MODULE_FLOOR, (
                f"Per-module floor for {entry['path']} was lowered to {entry['floor']}. "
                f"Expected >= {MIN_PER_MODULE_FLOOR}. Lower MIN_PER_MODULE_FLOOR in the same PR "
                f"if the drop is intentional."
            )
