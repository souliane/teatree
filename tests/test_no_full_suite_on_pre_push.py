"""The pre-push stage must never run the FULL local test suite (#112/#21/#38).

push -> CI is the gate. A host under load times out unrelated wall-clock and
concurrency tests (e.g. test_simultaneous_fresh_starts_never_both_claim,
test_two_worktrees_provision_serve_concurrently, test_cli_dogfood) and blocks
the push. These tests pin that no push-stage hook in .pre-commit-config.yaml
invokes an UNSCOPED pytest run -- neither directly nor via a referenced script.

A PATH-SCOPED push pytest is allowed and pinned as such: the ``ci-critical-parity``
hook runs ``tests/quality`` + the never-lockout contract + ``--doctest-modules
src/teatree`` (fast static/AST + doctest collection), which cannot drag in the
wall-clock/concurrency suites the invariant forbids. ``TestCiCriticalParityHook``
guards that it stays scoped and cannot silently widen to the full suite.

The two whole-tree subprocess checks that dominated the push wall-clock -- the
~63s jscpd duplication scan (``tests/quality/test_jscpd_duplication.py``) and the
mutmut kill-proof run (``tests/quality/test_mutation_kill_proof.py``) -- carry a
``push_heavy`` marker and are DESELECTED at push (``-m "not push_heavy"``). They
are RELOCATED to CI, not dropped: CI's ``test-shard`` lane runs the whole suite
with NO marker filter, so both still gate on every PR. ``TestPushHeavyRelocatedToCI``
pins that relocation -- excluded at push, present in CI.
"""

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_HEAVY_FILES = (
    _REPO_ROOT / "tests" / "quality" / "test_jscpd_duplication.py",
    _REPO_ROOT / "tests" / "quality" / "test_mutation_kill_proof.py",
)

# A pytest invocation with no path/marker scoping -- the full-suite signature.
# Matches "pytest", "uv run pytest", "uv run -p 3.13 pytest" not followed by a
# path/marker argument on the same logical command.
_BARE_PYTEST = re.compile(r"\bpytest\b(?!\s+\S*(?:\.py|::|-k\b|-m\b|tests/|src/))")


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
        offenders = [h for h in _push_hooks() if _BARE_PYTEST.search(h.get("entry") or "")]
        assert not offenders, (
            "pre-push hook(s) invoke an UNSCOPED pytest -- the full suite belongs in "
            f"CI, not the local push path: {[h.get('id') for h in offenders]}"
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
                if _BARE_PYTEST.search(body):
                    offenders.append(f"{hook.get('id')} -> {entry[0]}")
        assert not offenders, (
            "pre-push hook script(s) run an unscoped pytest suite -- push -> CI "
            f"is the gate, not the local suite: {offenders}"
        )


class TestCiCriticalParityHook:
    """Pin the ``ci-critical-parity`` push hook stays PATH-SCOPED and complete.

    It closes the local/CI divergence (doctest + never-lockout classes) at push
    time WITHOUT running the full suite -- so it must (a) exist on the push stage,
    (b) be path-scoped (its pytest is not bare), (c) still carry its three
    load-bearing targets, and (d) DESELECT the ``push_heavy`` whole-tree checks so
    the push gate stays light. A future edit can neither widen it to the full
    suite nor silently drop the doctest / never-lockout coverage.
    """

    def _hook(self) -> dict:
        matches = [h for h in _push_hooks() if h.get("id") == "ci-critical-parity"]
        assert matches, "ci-critical-parity push hook is missing"
        return matches[0]

    def test_entry_is_not_a_bare_full_suite(self) -> None:
        assert not _BARE_PYTEST.search(self._hook()["entry"]), (
            "ci-critical-parity widened to an unscoped pytest -- it must stay path-scoped so the "
            "no-full-suite-on-push invariant holds."
        )

    def test_entry_keeps_its_three_load_bearing_targets(self) -> None:
        entry = self._hook()["entry"]
        for token in ("tests/quality", "tests/test_gate_never_lockout_contract.py", "--doctest-modules src/teatree"):
            assert token in entry, f"ci-critical-parity dropped `{token}` -- it must not narrow its coverage."

    def test_entry_deselects_push_heavy(self) -> None:
        # The whole-tree jscpd + mutmut checks must be OFF the push gate; the
        # marker deselection is what keeps the ~63s scan out of every push.
        assert "not push_heavy" in self._hook()["entry"], (
            "ci-critical-parity must deselect the `push_heavy` whole-tree checks "
            '(`-m "not push_heavy"`) so the jscpd/mutmut scans do not gate a push.'
        )


def _ci_shard_pytest() -> str:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text())
    steps = workflow["jobs"]["test-shard"]["steps"]
    runs = [str(step["run"]) for step in steps if "run" in step and "pytest" in str(step.get("run", ""))]
    assert runs, "test-shard job runs no pytest step"
    return "\n".join(runs)


class TestPushHeavyRelocatedToCI:
    """The two whole-tree checks are OFF the push gate but STILL run in CI.

    The invariant is relocation, not deletion: nothing that gated before the
    push-hook slim-down is ungated after -- the jscpd + mutmut checks simply move
    from push-time to CI-time. This class pins all four halves of that contract:
    the checks carry the marker, the marker is registered (``--strict-markers``),
    the push hook deselects it, and CI's shard lane does NOT.
    """

    def test_push_heavy_marker_is_registered(self) -> None:
        markers = _PYPROJECT.read_text()
        # --strict-markers rejects an unregistered marker; the checks would ERROR
        # at collection if the marker were applied but not declared here.
        assert '"push_heavy:' in markers, (
            "the `push_heavy` marker must be registered in pyproject.toml "
            "[tool.pytest.ini_options] markers -- --strict-markers rejects it otherwise."
        )

    def test_heavy_checks_carry_the_marker(self) -> None:
        for path in _HEAVY_FILES:
            source = path.read_text()
            assert re.search(r"pytest\.mark\.push_heavy", source), (
                f"{path.relative_to(_REPO_ROOT).as_posix()} must carry the `push_heavy` marker "
                "so the push hook can deselect its whole-tree scan."
            )

    def test_ci_shard_lane_does_not_deselect_push_heavy(self) -> None:
        # Relocation proof: the shard lane runs the WHOLE suite with no marker
        # filter, so a `push_heavy`-marked check still gates on every PR.
        shard = _ci_shard_pytest()
        assert "push_heavy" not in shard, (
            "CI's test-shard lane must NOT filter out `push_heavy` -- the heavy checks "
            "are relocated to CI, not deleted, so the shard must still run them."
        )
