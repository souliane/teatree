# test-path: cross-cutting — asserts over .pre-commit-config.yaml + ci.yml + pyproject.toml; no src/teatree/ mirror.
"""The pre-push stage must never run the FULL local test suite (#112/#21/#38).

push -> CI is the gate. A host under load times out unrelated wall-clock and
concurrency tests (e.g. test_simultaneous_fresh_starts_never_both_claim,
test_two_worktrees_provision_serve_concurrently, test_cli_dogfood) and blocks
the push. These tests pin that no push-stage hook in .pre-commit-config.yaml
invokes an UNSCOPED pytest run -- neither directly nor via a referenced script. The
matcher lives in ``teatree.quality.full_suite_invocation`` (own tests in
``tests/teatree_quality/test_full_suite_invocation.py``), shared with the per-phase
mandate guard ``teatree.quality.local_verification`` so the two cannot drift.

A PATH-SCOPED push gate is allowed and pinned as such: the ``ci-critical-parity``
hook runs ``dev/push-gate.sh`` (#122), which runs the never-lockout contract, the
conformance TOTALITY CORE, and the incremental push gate (``t3 tool push-gate --run``
-- the scoped doctest + scoped ast-grep regression scan, FULL on any uncertainty).
None can drag in the wall-clock/concurrency suites the invariant forbids.
``TestCiCriticalParityHook`` guards that the gate stays scoped, keeps the
never-lockout contract, and cannot silently widen back to the full suite.

The conformance core is the one lane here whose cost is a WHOLE-TREE walk, so it is
pinned three ways (#172): every pytest invocation in the script is serial (``-n auto``
replicates the tree parse per worker and is what took the whole directory from 34s
on an idle box to 19+ minutes in a memory-capped container), the core runs under a
hard ``timeout`` of at most two minutes whose expiry defers to CI while any other
failure still fails the push, and every listed file is static introspection only
(no subprocess, no clock, no DB, no ``tests/``-tree parse) -- the classes measured
to cost 8-29s per file or to assert the machine rather than the tree.

The broad ``tests/quality`` directory is CI-only: even with ``push_heavy`` deselected
its ~666 subprocess-spawning tests ran ~420s locally (``-n auto``), dwarfing the
gate's whole point (a fast early signal) and hitting the push-hook wall-clock cap.
CI's ``test-shard`` lane runs it whole-tree on every PR, so relocating it off the
push path loses zero coverage. ``TestPushHeavyRelocatedToCI`` still pins that the
three heaviest CLASSES -- the ~63s jscpd scan (``TestScanCoverage``), the mutmut
kill-proof run (``TestMutmutKillsTheMutant``), and the >300s ast-grep whole-tree scan
(``TestBlockingSetIsGreen``) -- carry the ``push_heavy`` marker (so a scoped local run
can deselect them with ``-m "not push_heavy"``) while the cheap siblings do not, and
that CI's shard lane runs ALL of them with no marker filter.

The "no full suite on push" invariant is STRICTLY MORE satisfied by #122 (the push
runs strictly fewer tests), never weakened: no must-block test is inverted, and the
safety net moves from "whole-tree at push" to "never-lockout + scoped-gate at push,
whole-tree at CI, plus the CI selection-audit".
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from teatree.quality.full_suite_invocation import declared_testpaths, runs_full_suite

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Per heavy file: the CLASS that runs the expensive subprocess (marked
# `push_heavy`, deselected at push) vs the fast/deterministic class that must
# stay SELECTED at push. Marking the whole module would lift the cheap classes
# off the push gate too, losing fast feedback on a config / manual-mutant regression.
_HEAVY_CHECKS = {
    _REPO_ROOT / "tests" / "quality" / "test_jscpd_duplication.py": {
        "heavy": ("TestScanCoverage",),
        "cheap": ("TestConfigPin",),
    },
    _REPO_ROOT / "tests" / "quality" / "test_mutation_kill_proof.py": {
        "heavy": ("TestMutmutKillsTheMutant",),
        "cheap": ("TestManualMutantKilled",),
    },
    # #122: the >300s whole-tree ast-grep scan moves off the push gate; the fast
    # manifest-schema class stays SELECTED at push for quick config-regression feedback.
    _REPO_ROOT / "tests" / "quality" / "test_regression_rules.py": {
        "heavy": ("TestBlockingSetIsGreen",),
        "cheap": ("TestManifestSchema",),
    },
}


_TESTPATHS_ROOTS = declared_testpaths(_PYPROJECT)

#: Owner ruling on #172: the push gate's conformance step may never exceed two minutes.
_CONFORMANCE_CAP_CEILING_S = 120

#: The route/registry totality lanes the push-time core exists for (#3787, #3788).
_TOTALITY_ANCHORS = frozenset(
    {
        "test_signal_route_totality.py",
        "test_registry_parity.py",
        "test_registry_parity_roster.py",
        "test_gate_registry_walk.py",
        "test_capability_surface_walk.py",
        "test_consumer_caller_walk.py",
    }
)

_CLOCK_READS = frozenset({"perf_counter", "monotonic", "perf_counter_ns", "monotonic_ns"})
_DB_MARKERS = frozenset({"django_db", "TestCase", "TransactionTestCase", "transaction"})
_TESTS_TREE_NAMES = frozenset({"_TESTS_DIR", "_TESTS_ROOT"})


def machine_dependencies(path: Path) -> list[str]:
    """Why *path* measures the machine instead of the tree; empty for a pure src walk."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])
    }
    reasons = []
    if "subprocess" in imported:
        reasons.append("spawns subprocesses")
    if (names | attributes) & _CLOCK_READS:
        reasons.append("reads the wall clock")
    if (names | attributes) & _DB_MARKERS:
        reasons.append("uses the database")
    if names & _TESTS_TREE_NAMES or any(
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and node.right.value == "tests"
        for node in ast.walk(tree)
    ):
        reasons.append("parses the tests tree")
    return reasons


def _push_hooks() -> list[dict]:
    config = yaml.safe_load(_CONFIG.read_text())
    default_stages = set(config.get("default_stages", []))
    push = []
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            stages = set(hook.get("stages", default_stages))
            # prek treats "push" and "pre-push" as the same stage.
            if stages & {"push", "pre-push"}:
                push.append(hook)
    return push


def _ci_critical_parity_script_body() -> str:
    matches = [h for h in _push_hooks() if h.get("id") == "ci-critical-parity"]
    assert matches, "ci-critical-parity push hook is missing"
    entry = matches[0]["entry"].split()
    script = _REPO_ROOT / entry[0]
    assert script.is_file(), f"ci-critical-parity entry {entry[0]!r} must resolve to a repo script"
    return script.read_text()


def _declared_conformance_core(body: str) -> list[Path]:
    block = re.search(r"^conformance_core=\(\n(.*?)^\)$", body, re.MULTILINE | re.DOTALL)
    assert block, "dev/push-gate.sh must declare the conformance core as a `conformance_core=(...)` array"
    return [_REPO_ROOT / line.strip() for line in block.group(1).splitlines() if line.strip()]


class TestNoFullSuiteOnPrePush:
    def test_config_has_push_hooks(self) -> None:
        # Guard the guard: if the push stage is empty the assertions below are
        # vacuous, so a renamed stage key can't silently pass this file.
        assert _push_hooks(), "expected push-stage hooks in .pre-commit-config.yaml"

    def test_no_push_hook_runs_unscoped_pytest_directly(self) -> None:
        # A SCOPED pytest (path/marker after `pytest`) is allowed; only a BARE,
        # unscoped `pytest` (the full-suite signature) is forbidden on the push path.
        offenders = [h for h in _push_hooks() if runs_full_suite(h.get("entry") or "", _TESTPATHS_ROOTS)]
        assert not offenders, (
            "pre-push hook(s) invoke an UNSCOPED pytest -- the full suite belongs in "
            f"CI, not the local push path: {[h.get('id') for h in offenders]}"
        )

    def test_widened_script_running_testpaths_root_is_rejected(self) -> None:
        # The regression: `pyproject` sets testpaths=["tests"], so `uv run pytest
        # tests/` IS the full suite. A push-gate script widened to that must be
        # REJECTED by the guard -- a trailing slash must not exempt it.
        widened = '#!/usr/bin/env bash\nset -euo pipefail\necho "=== running the suite ==="\nuv run pytest tests/\n'
        assert runs_full_suite(widened, _TESTPATHS_ROOTS), (
            "guard is blind to `pytest tests/` -- that IS the whole suite (testpaths=['tests'])"
        )

    def test_no_push_hook_script_runs_full_suite(self) -> None:
        # A push hook may shell out to a script; that script must not run the
        # unscoped suite either. Resolve `entry` to a repo file when it is one.
        offenders: list[str] = []
        for hook in _push_hooks():
            entry = (hook.get("entry") or "").split()
            if not entry:
                continue
            candidate = _REPO_ROOT / entry[0]
            if candidate.is_file():
                body = candidate.read_text()
                if runs_full_suite(body, _TESTPATHS_ROOTS):
                    offenders.append(f"{hook.get('id')} -> {entry[0]}")
        assert not offenders, (
            "pre-push hook script(s) run an unscoped pytest suite -- push -> CI "
            f"is the gate, not the local suite: {offenders}"
        )


class TestDuplicationRatchetIsRelocatedNotRemoved:
    def _jscpd_hook(self) -> dict:
        config = yaml.safe_load(_CONFIG.read_text())
        matches = [
            hook for repo in config.get("repos", []) for hook in repo.get("hooks", []) if hook.get("id") == "jscpd"
        ]
        assert len(matches) == 1, "expected exactly one jscpd hook"
        return matches[0]

    def test_jscpd_runs_at_commit_not_push(self) -> None:
        assert self._jscpd_hook().get("stages") == ["commit"]
        assert "jscpd" not in {hook.get("id") for hook in _push_hooks()}

    def test_ci_still_runs_the_commit_stage_over_all_files(self) -> None:
        assert "prek run --all-files" in _CI_WORKFLOW.read_text(encoding="utf-8")


class TestCiCriticalParityHook:
    """Pin the ``ci-critical-parity`` push hook stays PATH-SCOPED and complete (#122).

    The hook runs ``dev/push-gate.sh``, which keeps a fast early signal at push time
    WITHOUT the full suite -- so it must (a) exist on the push stage pointing at the
    script, (b) keep the script path-scoped (no bare pytest), (c) keep the
    load-bearing targets in the SCRIPT (the never-lockout contract and the
    doctest+ast-grep engine via ``t3 tool push-gate``), (d) keep the ~420s
    ``tests/quality`` dir OFF the push path (CI-only, covered whole-tree by the
    ``test (3.13)`` shard), and (e) keep the conformance core serial, capped at two
    minutes, anchored on the route/registry totality lanes, and free of any file
    whose cost or verdict depends on the machine (#172). A future edit can neither
    widen it to the full suite nor silently drop the never-lockout / incremental-gate
    coverage.

    This is a documented RE-SPEC of the old inline-entry contract, not a weakening:
    the "no full suite on push" invariant (``TestNoFullSuiteOnPrePush``) is strictly
    MORE satisfied (the push runs strictly fewer tests), and no must-block assertion
    is inverted -- the ``tests/quality`` coverage is RELOCATED to CI, not dropped.
    """

    def _hook(self) -> dict:
        matches = [h for h in _push_hooks() if h.get("id") == "ci-critical-parity"]
        assert matches, "ci-critical-parity push hook is missing"
        return matches[0]

    def _script_body(self) -> str:
        return _ci_critical_parity_script_body()

    def test_entry_points_at_the_push_gate_script(self) -> None:
        assert "dev/push-gate.sh" in self._hook()["entry"], (
            "ci-critical-parity must run dev/push-gate.sh (the #122 scoped push gate)."
        )

    def test_script_is_not_a_bare_full_suite(self) -> None:
        assert not runs_full_suite(self._script_body(), _TESTPATHS_ROOTS), (
            "dev/push-gate.sh widened to an unscoped pytest -- it must stay path-scoped so the "
            "no-full-suite-on-push invariant holds."
        )

    def test_script_keeps_its_load_bearing_targets(self) -> None:
        body = self._script_body()
        for token in ("tests/test_gate_never_lockout_contract.py", "t3 tool push-gate"):
            assert token in body, f"dev/push-gate.sh dropped `{token}` -- it must not narrow its coverage."

    def _run_lines(self) -> list[str]:
        return [line for line in self._script_body().splitlines() if line.strip() and not line.lstrip().startswith("#")]

    def _conformance_core(self) -> list[Path]:
        return _declared_conformance_core(self._script_body())

    def test_every_pytest_invocation_is_serial(self) -> None:
        # `-n auto` pays the whole-tree parse once PER WORKER: measured 1.2 GB per process
        # for the tests-tree walk, six workers into a 5.2 GiB container, 19+ minutes (#172).
        pytest_lines = [line for line in self._run_lines() if "run pytest" in line]
        assert pytest_lines, "dev/push-gate.sh runs no pytest at all"
        offenders = [line for line in pytest_lines if "-n 0" not in line]
        assert not offenders, (
            f"dev/push-gate.sh pytest invocation(s) without `-n 0` -- xdist multiplies the tree parse: {offenders}"
        )

    def test_conformance_core_runs_under_a_two_minute_cap(self) -> None:
        body = self._script_body()
        cap = re.search(r"^conformance_cap_s=(\d+)$", body, re.MULTILINE)
        assert cap, "dev/push-gate.sh must pin the conformance cap as `conformance_cap_s=<seconds>`"
        assert int(cap.group(1)) <= _CONFORMANCE_CAP_CEILING_S, (
            f"conformance cap {cap.group(1)}s exceeds the {_CONFORMANCE_CAP_CEILING_S}s ceiling (owner ruling, #172)"
        )
        capped = [
            line
            for line in self._run_lines()
            if line.startswith('"$timeout_bin" --kill-after=10s "$conformance_cap_s"')
            and 'run pytest "${conformance_core[@]}"' in line
        ]
        assert capped, (
            'the conformance core must run as `"$timeout_bin" --kill-after=10s "$conformance_cap_s" '
            '... run pytest "${conformance_core[@]}"`'
        )

    def test_timeout_binary_is_resolved_to_gtimeout_or_timeout(self) -> None:
        # macOS ships no `timeout(1)`; Homebrew's coreutils installs it as `gtimeout`
        # unless the gnubin directory leads PATH. Probing only `timeout` blocked every
        # such push with a remediation message that did not fix it (#172 review).
        body = self._script_body()
        assert re.search(r"^for candidate in timeout gtimeout; do$", body, re.MULTILINE), (
            "dev/push-gate.sh must probe both `timeout` and `gtimeout` before refusing to run"
        )
        assert '"$timeout_bin"' in body, "dev/push-gate.sh must invoke the resolved binary, not a literal `timeout`"

    def test_only_a_cap_expiry_defers_to_ci(self) -> None:
        # Exit 124 is timeout(1)'s own code; every other non-zero is a real verdict and
        # must still refuse the push, or the cap would turn a red lane into a green push.
        body = self._script_body()
        assert re.search(r'^if \[ "\$conformance_rc" -eq 124 \]; then$', body, re.MULTILINE), (
            "the deferral branch must test exactly exit 124 (timeout expiry)"
        )
        assert re.search(
            r'^elif \[ "\$conformance_rc" -ne 0 \]; then\n\s+exit "\$conformance_rc"$', body, re.MULTILINE
        ), "a non-timeout conformance failure must propagate its exit code and fail the push"

    def test_conformance_core_lists_the_totality_anchors(self) -> None:
        # The class that reached CI twice (#3787, #3788): a registered producer with no
        # live consumer/route. These lanes ARE the early signal; dropping one from the
        # core is dropping the reason the core exists.
        listed = {path.name for path in self._conformance_core()}
        missing = sorted(_TOTALITY_ANCHORS - listed)
        assert not missing, f"conformance core dropped totality anchor(s): {missing}"
        absent = [path for path in self._conformance_core() if not path.is_file()]
        assert not absent, f"conformance core names files that do not exist: {absent}"

    def test_conformance_core_is_static_introspection_only(self) -> None:
        offenders = {path.name: reasons for path in self._conformance_core() if (reasons := machine_dependencies(path))}
        assert not offenders, (
            "conformance core file(s) depend on the machine rather than the tree -- they belong in CI, "
            f"where the whole directory runs on every MR: {offenders}"
        )

    def test_script_does_not_run_the_heavy_quality_dir(self) -> None:
        # #122: the broad `tests/quality` dir is CI-only (its ~666 subprocess tests
        # ran ~420s locally even with `push_heavy` deselected, hitting the push-hook
        # wall-clock cap). Relocating it to CI's `test (3.13)` shard loses no coverage
        # and makes this gate an actually-fast early signal. Pin it OFF the push path
        # so the 420s dir can never silently return -- strictly stronger than the old
        # `-m "not push_heavy"` deselection this replaces. A comment may NAME the dir
        # to explain why it is CI-only, so only executable (non-comment) lines are checked.
        run_lines = [
            line for line in self._script_body().splitlines() if line.strip() and not line.lstrip().startswith("#")
        ]
        offenders = [line for line in run_lines if "tests/quality" in line]
        assert not offenders, (
            "dev/push-gate.sh must NOT run the `tests/quality` directory -- it is CI-only "
            f"(covered whole-tree by the `test (3.13)` shard); running it at push blows the wall-clock cap: {offenders}"
        )


@pytest.mark.push_heavy
@pytest.mark.timeout(200)
class TestConformanceCoreStaysUnderBudget:
    """Actually running the core is the only check a new COST class cannot fool (#172 review).

    ``test_conformance_core_is_static_introspection_only`` only rejects the four
    named machine-dependency classes (subprocess, clock, DB, tests-tree parse).
    Verified by planting: adding ``test_user_settings_readers.py`` -- an
    O(fields x src-files) walk with none of those four classes, measured at 35s+
    of call time here -- to ``conformance_core`` left every other guard green.
    Running the declared core once and budgeting PER FILE is the only check that
    cost shape cannot slip past. Deselected at push (`-m "not push_heavy"`) like
    the other whole-tree checks in this repo; it runs in CI.
    """

    #: Every currently-listed file measures well under a second of call+setup
    #: time here; the file excluded above for cost measured 35s+. An order of
    #: magnitude of headroom over the former, comfortably under the latter.
    _PER_FILE_BUDGET_S = 15.0
    _DURATION_LINE = re.compile(r"^(?P<seconds>\d+\.\d+)s\s+(?:call|setup|teardown)\s+(?P<nodeid>\S+)$")

    def test_each_core_file_stays_under_its_budget(self) -> None:
        core = _declared_conformance_core(_ci_critical_parity_script_body())
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *(str(path) for path in core), "-n", "0", "-q", "--durations=0"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=170,
            check=False,
        )
        assert proc.returncode == 0, f"the declared conformance core must pass:\n{proc.stdout}\n{proc.stderr}"
        totals: dict[str, float] = {}
        for line in proc.stdout.splitlines():
            match = self._DURATION_LINE.match(line.strip())
            if not match:
                continue
            file_path = match.group("nodeid").split("::", 1)[0]
            totals[file_path] = totals.get(file_path, 0.0) + float(match.group("seconds"))
        offenders = {path: round(seconds, 1) for path, seconds in totals.items() if seconds > self._PER_FILE_BUDGET_S}
        assert not offenders, (
            f"conformance core file(s) exceed the {self._PER_FILE_BUDGET_S}s per-file budget "
            f"(a candidate this slow belongs in CI, not the push-time core): {offenders}"
        )


class TestMachineDependencyDetectorFiresRed:
    """Anti-vacuity: the composition rule must SEE each class it excludes from the core."""

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("test_scope_dependent_callers.py", "parses the tests tree"),
            ("test_session_start_latency.py", "reads the wall clock"),
            ("test_checkout_disposal_deciders_refuse_a_foreign_view.py", "spawns subprocesses"),
            ("test_loop_classification.py", "uses the database"),
        ],
    )
    def test_a_known_machine_dependent_lane_is_named(self, name: str, reason: str) -> None:
        assert reason in machine_dependencies(_REPO_ROOT / "tests" / "conformance" / name)

    def test_the_anchor_lanes_are_clean(self) -> None:
        dirty = {name: machine_dependencies(_REPO_ROOT / "tests" / "conformance" / name) for name in _TOTALITY_ANCHORS}
        assert not any(dirty.values()), dirty


def _ci_shard_pytest() -> str:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text())
    steps = workflow["jobs"]["test-shard"]["steps"]
    runs = [str(step["run"]) for step in steps if "run" in step and "pytest" in str(step.get("run", ""))]
    assert runs, "test-shard job runs no pytest step"
    return "\n".join(runs)


def _class_decorators(source: str) -> dict[str, list[str]]:
    return {
        node.name: [ast.unparse(dec) for dec in node.decorator_list]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
    }


def _has_module_push_heavy(source: str) -> bool:
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            return "push_heavy" in ast.unparse(node.value)
    return False


class TestPushHeavyRelocatedToCI:
    """The heavy CLASSES are OFF the fast local lanes; the cheap ones stay ON; CI runs all.

    The invariant is relocation, not deletion: nothing that gated before is ungated
    after -- the jscpd + mutmut + whole-tree ast-grep checks run in CI's ``test-shard``
    lane on every PR. Since #122 the push gate no longer runs ``tests/quality`` at all,
    and since #3587 the fast inner loop (``dev/ci-parity-fast.sh``) runs the diff-scoped
    ``dev/test-affected.sh`` selector; the ``push_heavy`` marker keeps the three heaviest
    classes deselectable from any scoped local run (``-m "not push_heavy"``), CLASS-scoped
    so the cheap/deterministic siblings still run. This class pins every half of that
    contract: the marker is registered (``--strict-markers``), the heavy classes carry
    it, the cheap classes (and the module) do NOT, and CI's shard lane does NOT filter it.
    """

    def test_push_heavy_marker_is_registered(self) -> None:
        markers = _PYPROJECT.read_text()
        # --strict-markers rejects an unregistered marker; the checks would ERROR
        # at collection if the marker were applied but not declared here.
        assert '"push_heavy:' in markers, (
            "the `push_heavy` marker must be registered in pyproject.toml "
            "[tool.pytest.ini_options] markers -- --strict-markers rejects it otherwise."
        )

    def test_heavy_classes_carry_the_marker(self) -> None:
        for path, classes in _HEAVY_CHECKS.items():
            decorators = _class_decorators(path.read_text())
            for cls in classes["heavy"]:
                assert cls in decorators, f"{path.name}::{cls} not found -- update _HEAVY_CHECKS."
                assert "pytest.mark.push_heavy" in decorators[cls], (
                    f"{path.name}::{cls} runs the expensive subprocess and must be decorated "
                    "`@pytest.mark.push_heavy` so the push hook deselects it."
                )

    def test_cheap_classes_stay_fast_lane_selected(self) -> None:
        for path, classes in _HEAVY_CHECKS.items():
            source = path.read_text()
            assert not _has_module_push_heavy(source), (
                f"{path.name} must NOT carry a module-level `push_heavy` pytestmark -- that would "
                "lift the fast config-pin / manual-mutant checks off the inner-loop lane too."
            )
            decorators = _class_decorators(source)
            for cls in classes["cheap"]:
                assert cls in decorators, f"{path.name}::{cls} not found -- update _HEAVY_CHECKS."
                assert "pytest.mark.push_heavy" not in decorators[cls], (
                    f"{path.name}::{cls} is fast + deterministic and must stay SELECTED in the fast "
                    "inner-loop lane -- it must not carry the `push_heavy` marker."
                )

    def test_ci_shard_lane_does_not_deselect_push_heavy(self) -> None:
        # Relocation proof: the shard lane runs the WHOLE suite with no marker
        # filter, so a `push_heavy`-marked check still gates on every PR.
        shard = _ci_shard_pytest()
        assert "push_heavy" not in shard, (
            "CI's test-shard lane must NOT filter out `push_heavy` -- the heavy checks "
            "are relocated to CI, not deleted, so the shard must still run them."
        )
