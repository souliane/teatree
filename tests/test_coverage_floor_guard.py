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

The CI ``test (3.13)`` lane is sharded 4-way (``test-shard`` matrix) behind an
unchanged ``test`` combiner context. This guard is the safety-critical piece of
that change: the combiner floor is asserted LOAD-BEARING — the needs-edge to the
shards, the >= 2 distinct shard groups, the partition check, and the shard-pass
guard — so a future edit cannot quietly turn the 93% floor into a no-op (a
dropped shard, a collapsed matrix, or a combiner that never depends on the
shards must FAIL this guard).
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

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

# Flags the single-process local parity lane (``dev/test-cov.sh``) MUST carry.
# Dropping any silently weakens the local gate: ``--cov`` / ``--cov-branch`` stop
# measuring, ``--doctest-modules`` drops doctest coverage (which contributes to
# the floor), ``--cov-fail-under=93`` stops failing below the floor. The CI lane
# is sharded (measurement on the shards, floor on the combiner) and is locked
# separately by ``TestShardedCoverageLane`` below.
REQUIRED_COVERAGE_LANE_FLAGS: frozenset[str] = frozenset(
    {"--cov", "--cov-branch", "--doctest-modules", "--cov-fail-under=93"},
)

# Flags each CI shard MUST carry to measure fully AND split.
REQUIRED_SHARD_MEASURE_FLAGS: frozenset[str] = frozenset(
    {"--cov", "--cov-branch", "--doctest-modules", "--splits", "--group"},
)
# A shard measures only a QUARTER of the tree, so it must NOT enforce the 93%
# floor on its partial data (that is the combiner's job) — and it MUST explicitly
# neutralise the pyproject ``[tool.coverage.report] fail_under=93``, which
# pytest-cov auto-applies otherwise, failing every quarter-suite shard.
FORBIDDEN_SHARD_FLAG = "--cov-fail-under=93"
REQUIRED_SHARD_FLOOR_NEUTRALISER = "--cov-fail-under=0"


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


def _ci_jobs() -> dict:
    return yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _job_run_texts(job: dict) -> list[str]:
    """Whitespace-normalised text of every ``run:`` step in a job."""
    return [
        " ".join(str(step["run"]).split()) for step in job.get("steps", []) if isinstance(step, dict) and "run" in step
    ]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


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

    def test_local_coverage_lane_mirrors_ci(self) -> None:
        script = _COV_LANE_SCRIPT.read_text(encoding="utf-8")
        for flag in REQUIRED_COVERAGE_LANE_FLAGS:
            assert flag in script, (
                f"dev/test-cov.sh lost {flag!r}; it must stay the single-process CI-parity "
                f"lane so a developer can reproduce the coverage gate locally."
            )


class TestShardedCoverageLane:
    """Lock the sharded CI coverage lane so the 93% floor stays load-bearing.

    The required ``test (3.13)`` context is produced by the ``test`` COMBINER,
    which aggregates the 4-way ``test-shard`` matrix. Each assertion below pins
    one property that, if quietly removed, would turn the floor into a no-op —
    the exact anti-vacuity the FIX-CIRUNTIME plan calls the safety-critical edit.
    """

    def test_shard_lane_measures_fully(self) -> None:
        pytest_runs = [text for text in _job_run_texts(_ci_jobs()["test-shard"]) if "pytest" in text]
        assert pytest_runs, "the test-shard lane must invoke pytest"
        command = pytest_runs[0]
        for flag in REQUIRED_SHARD_MEASURE_FLAGS:
            assert flag in command, (
                f"the test-shard lane lost {flag!r}; each shard must measure exactly what "
                f"the old monolithic lane did (--cov/--cov-branch/--doctest-modules) AND "
                f"split (--splits/--group), or the combined floor is dishonest."
            )
        assert FORBIDDEN_SHARD_FLAG not in command, (
            "the test-shard lane must NOT enforce --cov-fail-under=93: a shard measures only a "
            "QUARTER of the tree, so floor-judging its partial data would be meaningless. "
            "The combiner enforces the floor once over the combined data."
        )
        assert REQUIRED_SHARD_FLOOR_NEUTRALISER in command, (
            "the test-shard lane must pass --cov-fail-under=0 to neutralise the pyproject "
            "`fail_under=93` config floor; without it pytest-cov applies the floor to the "
            "shard's partial data and every shard fails (verified: exit 1)."
        )

    def test_shard_matrix_declares_multiple_distinct_groups(self) -> None:
        # Anti-vacuity: collapsing the matrix to one group (or dropping it) would
        # make the "combiner" a no-op wrapper over a single un-sharded run — the
        # floor could then be silently disarmed by editing only the shard lane.
        groups = _ci_jobs()["test-shard"]["strategy"]["matrix"]["group"]
        assert len(set(groups)) >= 2, (
            f"the test-shard matrix must declare >= 2 distinct groups; got {groups}. "
            f"A single group defeats sharding and un-anchors the combiner floor."
        )

    def test_combiner_emits_the_required_context(self) -> None:
        jobs = _ci_jobs()
        assert "test" in jobs, "the required `test (3.13)` context must be produced by a job keyed `test`"
        python_versions = jobs["test"]["strategy"]["matrix"]["python-version"]
        assert python_versions == ["3.13"], (
            f"the combiner matrix must be python-version ['3.13'] so the emitted context stays "
            f"exactly `test (3.13)` (branch protection lists it by name); got {python_versions}."
        )

    def test_combiner_depends_on_the_shards(self) -> None:
        # Anti-vacuity (needs-edge): without this edge the combiner could report
        # `test (3.13)` green without the shards ever having run.
        assert "test-shard" in _needs(_ci_jobs()["test"]), (
            "the `test` combiner must `needs: test-shard`; dropping the edge would let the "
            "required context go green without the shards running."
        )

    def test_combiner_enforces_the_floor(self) -> None:
        joined = " ".join(_job_run_texts(_ci_jobs()["test"]))
        assert "coverage report --fail-under=93" in joined, (
            "the combiner lost `coverage report --fail-under=93`; the whole-tree 93% branch "
            "floor moved onto the combiner when the lane was sharded — dropping it disarms the gate."
        )
        assert "t3 ci coverage" in joined, (
            "the combiner lost `t3 ci coverage`; the per-module floors must still run over the combined data."
        )

    def test_combiner_asserts_an_exact_partition(self) -> None:
        # A dropped or duplicated shard must red the required context, not ride a
        # green coverage number: the combiner runs the completeness checker first.
        joined = " ".join(_job_run_texts(_ci_jobs()["test"]))
        assert "check_shard_completeness.py" in joined, (
            "the combiner must run scripts/ci/check_shard_completeness.py so a silently-dropped "
            "shard (sum<total) or a duplicated group (sum>total) fails LOUD before the floor is trusted."
        )

    def test_combiner_fails_when_a_shard_failed(self) -> None:
        # A real test failure in a shard only ran a quarter of the suite; combined
        # coverage alone could pass, so the combiner must fail on any non-success shard.
        joined = " ".join(_job_run_texts(_ci_jobs()["test"]))
        assert "needs.test-shard.result" in joined, (
            "the combiner must fail when `needs.test-shard.result != success`; otherwise a "
            "failed shard could leave the required `test (3.13)` context green."
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
