"""The pre-push stage must never run the FULL local test suite (#112/#21/#38).

push -> CI is the gate. A host under load times out unrelated wall-clock and
concurrency tests (e.g. test_simultaneous_fresh_starts_never_both_claim,
test_two_worktrees_provision_serve_concurrently, test_cli_dogfood) and blocks
the push. These tests pin that no push-stage hook in .pre-commit-config.yaml
invokes an UNSCOPED pytest run -- neither directly nor via a referenced script.

A PATH-SCOPED push gate is allowed and pinned as such: the ``ci-critical-parity``
hook runs ``dev/push-gate.sh`` (#122), which runs the never-lockout contract and the
incremental push gate (``t3 tool push-gate --run`` -- the scoped doctest + scoped
ast-grep regression scan, FULL on any uncertainty). Neither can drag in the
wall-clock/concurrency suites the invariant forbids. ``TestCiCriticalParityHook``
guards that the gate stays scoped, keeps the never-lockout contract, and cannot
silently widen back to the full suite.

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
import shlex
import time
import tomllib
from pathlib import Path, PurePosixPath

import pytest
import yaml

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


def _testpaths_roots() -> tuple[str, ...]:
    # Deriving the forbidden root from `testpaths` means a directory rename moves
    # the root with it, instead of silently un-guarding the old name.
    ini = tomllib.loads(_PYPROJECT.read_text())["tool"]["pytest"]["ini_options"]
    return tuple(ini["testpaths"])


_TESTPATHS_ROOTS = _testpaths_roots()

# pytest/xdist options that consume the NEXT token as their value, so the value is
# never mistaken for a positional path (and a marker/keyword-only run with no path
# is correctly seen as collecting the whole `testpaths` tree).
_VALUE_OPTIONS = frozenset(
    {
        "-k",
        "-m",
        "-p",
        "-c",
        "-o",
        "-n",
        "-W",
        "-r",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--maxfail",
        "--rootdir",
        "--durations",
        "--dist",
        "--tx",
    }
)

# Shell tokens that end one command and begin the next.
_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})


def _split_on_separators(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _COMMAND_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    return segments


def _pytest_argvs(text: str) -> list[list[str]]:
    """The argv (tokens AFTER `pytest`) of every pytest invocation in `text`.

    Tokenised per line (via shlex, so quotes collapse and `#` comments drop) so a
    command boundary never lets one invocation absorb the next line's args; the
    first `pytest` token in a command segment is the runner. Linear in input size
    -- no backtracking regex, so a pathological argument cannot wedge the hook.
    """
    argvs: list[list[str]] = []
    for line in text.splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            tokens = line.split()
        argvs.extend(seg[seg.index("pytest") + 1 :] for seg in _split_on_separators(tokens) if "pytest" in seg)
    return argvs


def _positional_args(argv: list[str]) -> list[str]:
    positionals: list[str] = []
    consume_value = False
    for tok in argv:
        if consume_value:
            consume_value = False
            continue
        if tok.startswith("-"):
            consume_value = "=" not in tok and tok in _VALUE_OPTIONS
            continue
        positionals.append(tok)
    return positionals


def _is_testpaths_root(arg: str) -> bool:
    candidate = PurePosixPath(arg)
    return any(candidate == PurePosixPath(root) for root in _TESTPATHS_ROOTS)


def _runs_full_suite(command_text: str) -> bool:
    """Whether `command_text` runs pytest across the whole `testpaths` tree.

    True for a bare `pytest` (no positional path) or one whose positional path
    normalises to a `testpaths` root (`tests`, `tests/`, `./tests/`, quoted); a
    genuinely-scoped sub-path (`tests/quality`, `tests/foo.py::T`) returns False.
    """
    for argv in _pytest_argvs(command_text):
        positionals = _positional_args(argv)
        if not positionals or any(_is_testpaths_root(p) for p in positionals):
            return True
    return False


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


class TestNoFullSuiteOnPrePush:
    def test_config_has_push_hooks(self) -> None:
        # Guard the guard: if the push stage is empty the assertions below are
        # vacuous, so a renamed stage key can't silently pass this file.
        assert _push_hooks(), "expected push-stage hooks in .pre-commit-config.yaml"

    def test_no_push_hook_runs_unscoped_pytest_directly(self) -> None:
        # A SCOPED pytest (path/marker after `pytest`) is allowed; only a BARE,
        # unscoped `pytest` (the full-suite signature) is forbidden on the push path.
        offenders = [h for h in _push_hooks() if _runs_full_suite(h.get("entry") or "")]
        assert not offenders, (
            "pre-push hook(s) invoke an UNSCOPED pytest -- the full suite belongs in "
            f"CI, not the local push path: {[h.get('id') for h in offenders]}"
        )

    def test_widened_script_running_testpaths_root_is_rejected(self) -> None:
        # The regression: `pyproject` sets testpaths=["tests"], so `uv run pytest
        # tests/` IS the full suite. A push-gate script widened to that must be
        # REJECTED by the guard -- a trailing slash must not exempt it.
        widened = '#!/usr/bin/env bash\nset -euo pipefail\necho "=== running the suite ==="\nuv run pytest tests/\n'
        assert _runs_full_suite(widened), (
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
                if _runs_full_suite(body):
                    offenders.append(f"{hook.get('id')} -> {entry[0]}")
        assert not offenders, (
            "pre-push hook script(s) run an unscoped pytest suite -- push -> CI "
            f"is the gate, not the local suite: {offenders}"
        )


class TestFullSuiteMatcher:
    """`_runs_full_suite` rejects whole-`testpaths` runs, allows scoped ones.

    The bug this file guards against is a trailing slash (`pytest tests/`)
    sneaking the full suite past the gate.
    """

    @pytest.mark.parametrize(
        "invocation",
        [
            "pytest",
            "pytest tests",
            "pytest tests/",
            "pytest ./tests/",
            'pytest "tests/"',
            "pytest 'tests/'",
            "uv run pytest tests/",
            "python -m pytest tests",
            "uv run pytest tests//",
            'pytest -m "not push_heavy"',
        ],
    )
    def test_whole_suite_invocations_are_rejected(self, invocation: str) -> None:
        assert _runs_full_suite(invocation), f"{invocation!r} runs the whole suite but was not caught"

    @pytest.mark.parametrize(
        "invocation",
        [
            "pytest tests/quality",
            "pytest tests/test_gate_never_lockout_contract.py",
            "pytest tests/test_gate_never_lockout_contract.py::TestGate::test_x",
            'pytest tests/quality -m "not push_heavy"',
            'uv run pytest tests/quality tests/test_gate_never_lockout_contract.py -m "not push_heavy" -q',
            "pytest src/",
            "bash dev/push-gate.sh",
            "uv run t3 tool push-gate --run",
        ],
    )
    def test_scoped_invocations_are_allowed(self, invocation: str) -> None:
        assert not _runs_full_suite(invocation), f"{invocation!r} is genuinely scoped and must be allowed"

    def test_commented_invocation_is_not_an_invocation(self) -> None:
        # A `#`-commented mention documents the forbidden shape; it is not a run.
        assert not _runs_full_suite("# never do `uv run pytest tests/` on the push path")
        # ... but an inline comment must not mask a real run earlier on the line.
        assert _runs_full_suite("uv run pytest tests/  # early full-suite signal")

    def test_forbidden_root_is_derived_from_testpaths(self) -> None:
        # No hardcoded copy of "tests": the root tracks pyproject, so a rename
        # cannot leave the guard pinned to a stale directory name.
        declared = tomllib.loads(_PYPROJECT.read_text())["tool"]["pytest"]["ini_options"]["testpaths"]
        assert tuple(declared) == _TESTPATHS_ROOTS
        assert _TESTPATHS_ROOTS, "testpaths must be non-empty or the guard has no root to forbid"

    def test_matcher_is_redos_bounded(self) -> None:
        # This runs inside a git hook: a pathological argument must not wedge it.
        # The old backtracking lookahead was the exact ReDoS shape this pins shut.
        pathological = "pytest " + "tests/" * 10_000
        start = time.perf_counter()
        _runs_full_suite(pathological)
        assert time.perf_counter() - start < 0.5, "matcher is not linear -- possible ReDoS regression"


class TestCiCriticalParityHook:
    """Pin the ``ci-critical-parity`` push hook stays PATH-SCOPED and complete (#122).

    The hook runs ``dev/push-gate.sh``, which keeps a fast early signal at push time
    WITHOUT the full suite -- so it must (a) exist on the push stage pointing at the
    script, (b) keep the script path-scoped (no bare pytest), (c) keep the
    load-bearing targets in the SCRIPT (the never-lockout contract and the
    doctest+ast-grep engine via ``t3 tool push-gate``), and (d) keep the ~420s
    ``tests/quality`` dir OFF the push path (CI-only, covered whole-tree by the
    ``test (3.13)`` shard). A future edit can neither widen it to the full suite nor
    silently drop the never-lockout / incremental-gate coverage.

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
        entry = self._hook()["entry"].split()
        script = _REPO_ROOT / entry[0]
        assert script.is_file(), f"ci-critical-parity entry {entry[0]!r} must resolve to a repo script"
        return script.read_text()

    def test_entry_points_at_the_push_gate_script(self) -> None:
        assert "dev/push-gate.sh" in self._hook()["entry"], (
            "ci-critical-parity must run dev/push-gate.sh (the #122 scoped push gate)."
        )

    def test_script_is_not_a_bare_full_suite(self) -> None:
        assert not _runs_full_suite(self._script_body()), (
            "dev/push-gate.sh widened to an unscoped pytest -- it must stay path-scoped so the "
            "no-full-suite-on-push invariant holds."
        )

    def test_script_keeps_its_load_bearing_targets(self) -> None:
        body = self._script_body()
        for token in ("tests/test_gate_never_lockout_contract.py", "t3 tool push-gate"):
            assert token in body, f"dev/push-gate.sh dropped `{token}` -- it must not narrow its coverage."

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
