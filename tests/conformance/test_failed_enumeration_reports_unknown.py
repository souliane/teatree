"""A verdict is a claim about a set that was ENUMERATED — a failed scan is UNKNOWN.

``t3 tool triage-issues`` printed a clean backlog while both of its ``gh`` calls had
failed unauthenticated; a hand triage of the same repo at the same moment found
three resolved-but-open issues (souliane/teatree#4135). Each enumerator was
individually correct — it wrote the failure to stderr — and each verdict was
individually correct given its input. The defect is the seam: ``return []`` on a
failed read makes "the forge could not be reached" and "the backlog is clear" the
same bytes, and the verdict layer cannot tell them apart.

**Scope, stated plainly.** This lane is scoped to the triage tool surface
(``teatree/triage.py`` + its three ``t3 tool`` verdicts), NOT to the whole tree.
``src/teatree`` has ~34 sites that return a neutral value on a non-zero
``returncode``, and most are legitimate — a probe whose empty answer honestly means
"absent". A tree-wide property would need an allowlist longer than itself, which
would be a change detector wearing an invariant's clothes. What IS principled here
is the surface: every enumeration backing a ``t3 tool`` triage verdict, walked as a
family rather than fixed one offender at a time.

**What each lane is worth.** The substantive guarantee is structural: ``_gh_json`` is
the one chokepoint every enumeration reads through, and it raises. The two lanes below
guard that shape rather than restate it — no ``gh`` result is CONSUMED outside the
chokepoint (the two write calls discard theirs, and a discarded result cannot be
laundered into a verdict), and nothing catches :class:`ForgeEnumerationError` to hand
back an empty. Between them a new enumerator has no route to the silent-empty that does
not also fail a lane.

The ``returncode`` AST walk is narrower than either, and deliberately so: it recognises
the PRE-FIX SHAPE — a ``returncode``-guarded ``if`` returning a neutral empty — and
nothing else. A ternary, a ``stdout`` guard, a ``try``/``except`` swallow and an
assign-then-return all read clean to it. Widening it into a general dataflow analysis
would earn false positives across the ~34 legitimate neutral-empty returns named above,
for a shape the chokepoint lane already forecloses. It is a cheap regression pin on one
known defect, not the thing standing between the tree and the class.

The behavioural lanes pin the verdict layer end to end, including that
``--close-resolved`` cannot act on a scan that never ran.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.triage_tools import find_duplicates, label_issues, triage_issues
from teatree.triage import DuplicateFinder, ForgeEnumerationError, LabelSuggester, TriageScanner

_TRIAGE_MODULE = Path(__file__).resolve().parents[2] / "src" / "teatree" / "triage.py"
_TRIAGE_SOURCE = _TRIAGE_MODULE.read_text(encoding="utf-8")
_RUN_TARGET = "teatree.triage.run_allowed_to_fail"
_REPO = "souliane/teatree"

#: The single function every enumeration reads through, and the raise it makes.
_CHOKEPOINT = "_gh_json"
_ERROR = "ForgeEnumerationError"
_RUNNER = "run_allowed_to_fail"

#: Anti-vacuity floor: the walk must find the enumerators it claims to cover.
_MIN_RETURNCODE_BRANCHES = 1

_NEUTRAL_EMPTIES = (ast.List, ast.Dict, ast.Tuple, ast.Set)


def _failed_read() -> SimpleNamespace:
    return SimpleNamespace(stdout="", stderr="gh auth login required", returncode=1)


def _ok(payload: object) -> SimpleNamespace:
    return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)


def _laundering_returns(source: str) -> list[int]:
    """Line numbers where a ``returncode``-failure branch returns a neutral empty."""
    offenders: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If) or "returncode" not in ast.unparse(node.test):
            continue
        offenders.extend(
            stmt.lineno for stmt in ast.walk(node) if isinstance(stmt, ast.Return) and _is_neutral_empty(stmt.value)
        )
    return offenders


def _is_neutral_empty(value: ast.expr | None) -> bool:
    if value is None or (isinstance(value, ast.Constant) and value.value is None):
        return True
    return isinstance(value, _NEUTRAL_EMPTIES) and not getattr(value, "elts", getattr(value, "keys", None))


#: ``gh`` verbs that MUTATE. A call carrying one is not an enumeration, so consuming
#: its result cannot produce the silent-empty this rule exists to forbid — it is how a
#: write reports which items the forge refused, which is the opposite failure mode.
_WRITE_VERBS = frozenset({"close", "comment", "create", "delete", "edit", "reopen", "transfer"})


def _is_forge_write(node: ast.Call) -> bool:
    """True iff *node*'s argv literally names a mutating ``gh`` verb."""
    argv = node.args[0] if node.args else None
    if not isinstance(argv, ast.List):
        return False
    return any(isinstance(el, ast.Constant) and el.value in _WRITE_VERBS for el in argv.elts)


def _consumed_forge_reads(tree: ast.AST) -> list[int]:
    """Lines where a ``gh`` READ's result is USED outside the ``_gh_json`` chokepoint.

    A read whose result is discarded cannot reach a verdict; a read whose result is
    bound or returned can, and that is the route back to the silent-empty. A WRITE is
    exempt whether or not its result is consumed: it enumerates nothing, and consuming
    it is how the caller reports the items the forge refused.
    """
    chokepoint = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == _CHOKEPOINT)
    discarded = {
        node.value.lineno for node in ast.walk(tree) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    }
    inside = {node.lineno for node in ast.walk(chokepoint) if isinstance(node, ast.Call)}
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _RUNNER in ast.unparse(node.func)
        and node.lineno not in discarded
        and node.lineno not in inside
        and not _is_forge_write(node)
    )


def _swallowed_enumeration_errors(tree: ast.AST) -> list[int]:
    """Lines of every ``except`` handler that catches the fail-loud error."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is not None and _ERROR in ast.unparse(node.type)
    )


class TestTheChokepointIsTheOnlyRouteToAForgeRead:
    """The structural guarantee the narrow ``returncode`` walk does NOT provide."""

    def test_no_forge_read_is_consumed_outside_the_chokepoint(self) -> None:
        assert _consumed_forge_reads(ast.parse(_TRIAGE_SOURCE)) == []

    def test_the_lane_goes_red_on_an_enumerator_that_bypasses_the_chokepoint(self) -> None:
        """The control — an assign-then-return enumerator, one shape the walk cannot see."""
        bypass = _TRIAGE_SOURCE + (
            "\n\ndef fetch_extra(repo):\n"
            "    result = run_allowed_to_fail(['gh', 'issue', 'list'], expected_codes=None)\n"
            "    out = [] if result.returncode != 0 else json.loads(result.stdout)\n"
            "    return out\n"
        )
        assert _consumed_forge_reads(ast.parse(bypass))
        assert _laundering_returns(bypass) == []  # the narrow walk stays blind to it

    def test_nothing_swallows_the_fail_loud_error(self) -> None:
        assert _swallowed_enumeration_errors(ast.parse(_TRIAGE_SOURCE)) == []

    def test_the_lane_goes_red_on_a_handler_that_hands_back_an_empty(self) -> None:
        """The control — the fourth shape, a ``try``/``except`` swallow of the raise."""
        swallow = _TRIAGE_SOURCE + (
            "\n\ndef fetch_extra(repo):\n"
            "    try:\n"
            "        return _open_issues(repo)\n"
            "    except ForgeEnumerationError:\n"
            "        return []\n"
        )
        assert _swallowed_enumeration_errors(ast.parse(swallow))
        assert _laundering_returns(swallow) == []  # the narrow walk stays blind to it


class TestNoEnumeratorLaundersAFailedReadIntoAnEmptyOne:
    def test_the_walk_can_see_the_returncode_branches_it_covers(self) -> None:
        source = _TRIAGE_MODULE.read_text(encoding="utf-8")
        branches = [
            n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.If) and "returncode" in ast.unparse(n.test)
        ]
        assert len(branches) >= _MIN_RETURNCODE_BRANCHES

    def test_the_live_triage_surface_is_clean(self) -> None:
        assert _laundering_returns(_TRIAGE_MODULE.read_text(encoding="utf-8")) == []

    def test_the_walk_goes_red_on_the_pre_fix_shape(self) -> None:
        pre_fix = (
            "def fetch(self):\n"
            "    result = run(...)\n"
            "    if result.returncode != 0:\n"
            "        sys.stderr.write('gh issue list failed')\n"
            "        return []\n"
            "    return json.loads(result.stdout)\n"
        )
        assert _laundering_returns(pre_fix) == [5]

    def test_raising_on_the_failure_is_clean(self) -> None:
        fixed = (
            "def fetch(self):\n"
            "    result = run(...)\n"
            "    if result.returncode != 0:\n"
            "        raise ForgeEnumerationError('gh issue list failed')\n"
            "    return json.loads(result.stdout)\n"
        )
        assert _laundering_returns(fixed) == []


class TestEnumeratorsRaiseRatherThanReturnEmpty:
    def test_label_enumeration_raises(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()), pytest.raises(ForgeEnumerationError):
            LabelSuggester(_REPO).collect_suggestions()

    def test_duplicate_enumeration_raises(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()), pytest.raises(ForgeEnumerationError):
            DuplicateFinder(_REPO).find()

    def test_resolved_enumeration_raises(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()), pytest.raises(ForgeEnumerationError):
            TriageScanner(_REPO).find_resolved()

    def test_stale_enumeration_raises(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()), pytest.raises(ForgeEnumerationError):
            TriageScanner(_REPO).find_stale()

    def test_a_failed_pr_read_behind_a_good_issue_read_still_raises(self) -> None:
        """The #4135 half a single-call probe misses — the SECOND enumeration failing."""
        with (
            patch(
                _RUN_TARGET, side_effect=[_ok([{"number": 1, "title": "t", "body": "", "labels": []}]), _failed_read()]
            ),
            pytest.raises(ForgeEnumerationError),
        ):
            TriageScanner(_REPO).find_resolved()

    def test_a_genuinely_empty_backlog_is_still_empty(self) -> None:
        with patch(_RUN_TARGET, return_value=_ok([])):
            assert TriageScanner(_REPO).find_stale() == []


class TestTheVerdictLayerReportsUnknown:
    """The command must not print a clean verdict it never established."""

    def _run(self, command, *args: str):
        return CliRunner().invoke(_as_app(command), list(args))

    def test_triage_reports_unknown_and_exits_non_zero(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()):
            result = self._run(triage_issues, _REPO)
        assert result.exit_code == 1
        assert "UNKNOWN" in result.output
        assert "No resolved-but-open" not in result.output

    def test_close_resolved_cannot_act_on_a_scan_that_did_not_run(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()) as run:
            result = self._run(triage_issues, _REPO, "--close-resolved")
        assert result.exit_code == 1
        assert not [call for call in run.call_args_list if "close" in call.args[0]]

    def test_label_issues_reports_unknown(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()):
            result = self._run(label_issues, _REPO)
        assert result.exit_code == 1
        assert "No labelable issues" not in result.output

    def test_find_duplicates_reports_unknown(self) -> None:
        with patch(_RUN_TARGET, return_value=_failed_read()):
            result = self._run(find_duplicates, _REPO)
        assert result.exit_code == 1
        assert "No potential duplicates" not in result.output

    def test_a_genuine_clean_sweep_still_reports_none_found(self) -> None:
        with patch(_RUN_TARGET, return_value=_ok([])):
            result = self._run(triage_issues, _REPO)
        assert result.exit_code == 0
        assert "No resolved-but-open issues found." in result.output


def _as_app(command):
    import typer  # noqa: PLC0415 — test-local deferred import

    app = typer.Typer()
    app.command()(command)
    return app
