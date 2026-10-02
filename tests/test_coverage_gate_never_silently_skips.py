# test-path: cross-cutting — drives the hooks/scripts/coverage_gate.py PreToolUse gate; no src/teatree/ mirror.
"""HARD INVARIANT: gate 12 never declines to measure in SILENCE (#4004).

Gate 12 (§17.6.3) is deliberately fail-OPEN (#122) — a broken or ambiguous
environment must never wedge a merge-class create, and skipping beats denying
on evidence from an unrelated codebase. The hazard that buys is a decline
indistinguishable from a clean measurement: no deny, nothing on stderr, and the
gate reads as "measured, all good" while it is dark. That is how a rewrite of
``hooks/scripts/coverage_gate.py`` turned an unresolvable repo into total
suppression with the gate's own test file moving alongside it, leaving only
``test_gate_liveness_corpus.py`` to object.

So the fail-open contract stands and this file pins its price instead:

Every decline that did NOT measure names itself on stderr. A decline that DID
measure stays silent, so a ``NOTE`` line means exactly one thing — the gate did
not measure, and here is why. And the decline-path COUNT is pinned structurally,
so a rewrite adding a new fail-open branch cannot land without re-affirming the
invariant here.

Deliberately independent of the two surfaces that moved together in the
regression — the liveness corpus and ``test_block_uncovered_diff_hook.py``.
"""

import ast
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import coverage_gate
from hooks.scripts.coverage_gate import diff_coverage_finding
from hooks.scripts.hook_budget import HOOK_CEILING_S
from hooks.scripts.hook_router import handle_block_uncovered_diff
from tests._git_repo import make_git_repo, run_git

_SKIP_MARKER = "coverage gate 12 skipped"
_SHIP_BRANCH = "feat/widget"
_FAILING_REPORT = json.dumps({"passes": False, "uncovered": [{"path": "a.py", "lines": [1, 2]}]})
_PASSING_REPORT = json.dumps({"passes": True, "uncovered": [], "unreferenced_symbols": []})

# The gate's COMPLETE set of decline paths, across the two functions that own them.
# In ``coverage_finding_for_command``: an out-of-scope repo, ``t3`` off PATH, a spent
# hook budget (#4305), a crashed measurement — all four announce themselves, plus the
# verdict-shaped returns of ``diff_coverage_finding``. In ``handle_block_uncovered_diff``:
# "not a merge-class mutation" (silent by design — the gate was never in scope) and "no
# finding". A rewrite that adds a decline path changes a count and lands here,
# which is the whole point: the last rewrite added a silent one unnoticed.
_GATE_DECLINE_PATHS = 4
_HANDLER_DECLINE_PATHS = 2


def _shipping_repo(root: Path) -> Path:
    """A repo the gate can PROVE the ship comes from, so it reaches the measurement.

    Real git under ``tmp_path`` per the repo's test doctrine. Without a resolvable
    push destination the gate skips on scope alone and every assertion below would
    grade the wrong branch.
    """
    repo = make_git_repo(root / "session-clone")
    run_git(repo, "remote", "add", "origin", "https://example.invalid/my-org/my-repo.git")
    run_git(repo, "checkout", "-qb", _SHIP_BRANCH)
    return repo


def _create_in(repo: Path) -> dict:
    return {
        "session_id": "sess-4004",
        "tool_name": "Bash",
        "tool_input": {"command": f"gh pr create --head {_SHIP_BRANCH} --title t --body b"},
        "cwd": str(repo),
    }


@contextmanager
def _t3_reports(stdout: str, *, returncode: int = 0, raises: Exception | None = None) -> Iterator[None]:
    """Fake ONLY the ``t3 tool diff-coverage`` shellout; let every git probe run for real.

    ``coverage_gate`` and ``hook_router`` share one ``subprocess`` module object, so a
    blanket patch also answers the git reads the gate resolves its scope with — the
    gate would then skip before measuring and a silence assertion would pass vacuously.
    """
    real_run = subprocess.run

    def dispatch(argv, **kwargs):
        if not (isinstance(argv, list) and "diff-coverage" in argv):
            return real_run(argv, **kwargs)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="")

    with patch.object(router.subprocess, "run", side_effect=dispatch):
        yield


@pytest.fixture
def t3_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")


class _StepClock:
    """A monotonic clock that jumps *step* seconds between reads — a spent budget, instantly."""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def monotonic(self) -> float:
        now = self._now
        self._now += self._step
        return now


class TestEveryUnmeasuredDeclineAnnouncesItself:
    """Every fail-open branch, driven through the real handler."""

    def test_unresolvable_repo_notes_the_skip(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {
            "session_id": "sess-4004",
            "tool_name": "Bash",
            "tool_input": {"command": "cd /nonexistent/ship && gh pr create --title t --body b"},
        }
        assert handle_block_uncovered_diff(data) is False
        assert _SKIP_MARKER in capsys.readouterr().err

    def test_repo_that_ships_nothing_notes_the_skip(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        stranded = make_git_repo(tmp_path / "no-remote-clone")
        data = {
            "session_id": "sess-4004",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create --title t --body b"},
            "cwd": str(stranded),
        }
        assert handle_block_uncovered_diff(data) is False
        err = capsys.readouterr().err
        assert _SKIP_MARKER in err
        assert "-R" in err

    def test_missing_t3_notes_the_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(router.shutil, "which", lambda _: None)
        assert handle_block_uncovered_diff(_create_in(_shipping_repo(tmp_path))) is False
        err = capsys.readouterr().err
        assert _SKIP_MARKER in err
        assert "t3" in err

    def test_crashed_measurement_notes_the_skip(
        self, tmp_path: Path, t3_on_path: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _create_in(_shipping_repo(tmp_path))
        with _t3_reports("", raises=subprocess.TimeoutExpired(cmd="t3", timeout=30)):
            assert handle_block_uncovered_diff(data) is False
        assert _SKIP_MARKER in capsys.readouterr().err

    def test_a_spent_hook_budget_notes_the_skip(
        self,
        tmp_path: Path,
        t3_on_path: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """#4305: the ceiling is shared, so the gate can arrive with nothing left to spend.

        Starting a measurement anyway does not merely waste time — the harness
        cancels the overrunning hook and no decision is emitted at all.
        """
        monkeypatch.setattr(coverage_gate, "time", _StepClock(step=float(HOOK_CEILING_S)))
        with _t3_reports(_FAILING_REPORT, returncode=1):
            assert handle_block_uncovered_diff(_create_in(_shipping_repo(tmp_path))) is False
        assert _SKIP_MARKER in capsys.readouterr().err

    def test_unparsable_report_notes_the_skip(
        self, tmp_path: Path, t3_on_path: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _create_in(_shipping_repo(tmp_path))
        with _t3_reports("Traceback (most recent call last):\nModuleNotFoundError: coverage", returncode=1):
            assert handle_block_uncovered_diff(data) is False
        assert _SKIP_MARKER in capsys.readouterr().err

    def test_proven_different_target_notes_the_skip(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The most common decline of all — an explicit ``-R`` naming a different repo.

        This is the exact regression this file exists to catch: the rebuild onto
        #4001 imported ``_explicit_target_is_measured_repo`` verbatim and its
        own-repo decline came back with no ``note_gate_skipped`` call.
        """
        data = {
            "session_id": "sess-4004",
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr create -R other-org/other-repo --title t --description d"},
            "cwd": str(_shipping_repo(tmp_path)),
        }
        assert handle_block_uncovered_diff(data) is False
        err = capsys.readouterr().err
        assert _SKIP_MARKER in err
        assert "other-org/other-repo" in err


class TestReportShapesThatCarryNoVerdict:
    """``diff_coverage_finding`` returns ``None`` for four reasons; three are skips."""

    def test_non_object_report_notes_the_skip(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert diff_coverage_finding(json.dumps(["not", "a", "report"])) is None
        assert _SKIP_MARKER in capsys.readouterr().err

    def test_report_without_a_passes_verdict_notes_the_skip(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert diff_coverage_finding(json.dumps({"uncovered": []})) is None
        assert _SKIP_MARKER in capsys.readouterr().err

    def test_clean_verdict_stays_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert diff_coverage_finding(_PASSING_REPORT) is None
        assert capsys.readouterr().err == ""

    def test_genuine_finding_stays_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert diff_coverage_finding(_FAILING_REPORT) is not None
        assert capsys.readouterr().err == ""


class TestAMeasurementThatRanIsNeverReportedAsASkip:
    """The other half: a NOTE must mean "did not measure", so measuring emits none."""

    def test_clean_measurement_allows_without_a_note(
        self, tmp_path: Path, t3_on_path: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _create_in(_shipping_repo(tmp_path))
        with _t3_reports(_PASSING_REPORT):
            assert handle_block_uncovered_diff(data) is False
        assert _SKIP_MARKER not in capsys.readouterr().err

    def test_finding_denies_without_a_note(
        self, tmp_path: Path, t3_on_path: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _create_in(_shipping_repo(tmp_path))
        with _t3_reports(_FAILING_REPORT, returncode=1):
            assert handle_block_uncovered_diff(data) is True
        assert _SKIP_MARKER not in capsys.readouterr().err


def _constant_returns(module: ModuleType, function: str, *, value: object) -> list[int]:
    """Line numbers of every ``return <value>`` literal in *function* — its decline paths."""
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == function)
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is value
    ]


class TestDeclinePathCountIsPinned:
    """A new fail-open branch cannot be added without re-affirming the invariant.

    The regression this file exists for was a rewrite that added a silent decline
    while the gate's own tests moved with it. Counting the branches structurally,
    from an independent file, is what makes the next such rewrite land here.
    """

    def test_gate_declines_on_exactly_the_paths_this_file_covers(self) -> None:
        declines = _constant_returns(coverage_gate, "coverage_finding_for_command", value=None)
        assert len(declines) == _GATE_DECLINE_PATHS, (
            f"coverage_finding_for_command now declines on {len(declines)} paths (lines {declines}), not "
            f"{_GATE_DECLINE_PATHS}. A new fail-open branch must call `note_gate_skipped` and gain a case "
            "above before this count moves — a decline that says nothing is what #4004 exists to prevent."
        )

    def test_handler_keeps_only_the_trigger_and_the_no_finding_decline(self) -> None:
        declines = _constant_returns(router, "handle_block_uncovered_diff", value=False)
        assert len(declines) == _HANDLER_DECLINE_PATHS, (
            f"handle_block_uncovered_diff now declines on {len(declines)} paths (lines {declines}), not "
            f"{_HANDLER_DECLINE_PATHS}. Fail-open branches belong in `coverage_finding_for_command`, beside "
            "the `note_gate_skipped` that explains them — splitting them across modules is how one went silent."
        )


def _unnoted_false_returns(module: ModuleType, function: str) -> list[int]:
    """Line numbers of a literal ``return False`` with no note immediately before it.

    Flags a ``return False`` in *function* not immediately preceded by a
    ``note_gate_skipped(...)`` call in the SAME block. A secondary guard,
    narrower than the behavioral test above: it only sees a LITERAL ``False``
    return, not ``return <call-that-may-evaluate-False>`` (the exact shape the
    #4004 regression actually took, and why the regression test is the
    behavioral one — this AST check passes vacuously against it). What it DOES
    catch: the count guards above only reach `coverage_finding_for_command`
    and `handle_block_uncovered_diff`, which CALL the target-resolution
    functions but do not inline their bodies — so a future literal-``False``
    decline added inside ``_explicit_target_is_measured_repo`` or
    ``measured_repo_is_publish_target`` moves neither count and needs its own pin.
    """
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == function)
    unnoted: list[int] = []
    for parent in ast.walk(node):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            if not (
                isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant) and stmt.value.value is False
            ):
                continue
            prev = body[i - 1] if i > 0 else None
            noted = (
                isinstance(prev, ast.Expr)
                and isinstance(prev.value, ast.Call)
                and isinstance(prev.value.func, ast.Name)
                and prev.value.func.id == "note_gate_skipped"
            )
            if not noted:
                unnoted.append(stmt.lineno)
    return unnoted


class TestEveryDeclineIsImmediatelyPrecededByANote:
    """A structural pin reaching functions the count guard above cannot see.

    ``coverage_finding_for_command`` CALLS the two target-resolution functions
    below but does not inline their bodies, so its own count guard is blind to
    them. Catches a future literal ``return False`` decline with no adjacent
    note; the shape-agnostic regression pin is
    ``test_proven_different_target_notes_the_skip`` above, which drives the
    real handler end to end and fails on ANY silent-skip shape.
    """

    def test_explicit_target_resolution_has_no_unnoted_decline(self) -> None:
        unnoted = _unnoted_false_returns(coverage_gate, "_explicit_target_is_measured_repo")
        assert unnoted == [], (
            f"_explicit_target_is_measured_repo declines at line(s) {unnoted} with no preceding "
            "note_gate_skipped(...) call — a decline that says nothing is the #4004 regression shape."
        )

    def test_target_resolution_has_no_unnoted_decline(self) -> None:
        unnoted = _unnoted_false_returns(coverage_gate, "measured_repo_is_publish_target")
        assert unnoted == [], (
            f"measured_repo_is_publish_target declines at line(s) {unnoted} with no preceding "
            "note_gate_skipped(...) call — a decline that says nothing is the #4004 regression shape."
        )
