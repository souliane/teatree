# test-path: cross-cutting
"""Anti-vacuity RED scenarios R1-R7 — the safety-bias proof for the push gate (#122).

Each scenario is a diff that SHOULD trip a check but sits OUTSIDE a naive
affected-set; each must still force a whole-tree FULL run. The proof is the
DIVERGENCE from a naive selector (``_naive_astgrep_scope`` / ``_naive_doctest_scope``
— a scoper WITHOUT the FULL-trigger table): the naive selector would report a
scoped/empty green (the false green), while the real ``plan_push_gate`` forces
FULL. A test that passed on the naive selector would guard nothing, so the
divergence is the anti-vacuity receipt (``/t3:code`` § TDD).

R4 is the soundness PIN (not a divergence): it proves scoping doctests IS sound —
an unchanged module's stale docstring is main's/CI's concern, so the changed
module is the only doctest target and no import graph is needed.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.quality import push_gate as push_gate_mod
from teatree.quality.changed_set import ChangedSet, ChangedSetError, ChangeEntry
from teatree.quality.push_gate import (
    WHOLE_TREE_DOCTEST,
    PushGatePlan,
    _run_doctests,
    plan_push_gate,
    resolve_plan,
    run_push_gate,
)
from teatree.quality.regression_scan import AstGrepUnavailableError


def _changed(*entries: tuple[str, str]) -> ChangedSet:
    return ChangedSet(entries=tuple(ChangeEntry(status=s, path=p) for s, p in entries), base_ref="origin/main")


def _naive_astgrep_scope(changed: ChangedSet) -> list[str]:
    """A naive selector WITHOUT the FULL-trigger table — the stub the bias must beat.

    Scopes ast-grep to the changed ``.py`` files under src/tests only, ignoring
    status and every rule/config/data trigger. Whatever it does NOT list is
    silently un-scanned — the false-green this gate exists to forbid.
    """
    return [
        e.path
        for e in changed.entries
        if e.path.endswith(".py") and (e.path.startswith("src/teatree/") or e.path.startswith("tests/"))
    ]


def _naive_doctest_scope(changed: ChangedSet) -> list[str]:
    return [e.path for e in changed.entries if e.path.endswith(".py") and e.path.startswith("src/teatree/")]


class TestR1AstGrepRuleFlagsUntouchedFile:
    def test_astgrep_rule_edit_forces_full(self) -> None:
        offender = "src/teatree/core/overlay.py"  # an UNCHANGED file the edited rule now flags
        changed = _changed(("M", ".ast-grep/blocking/except-swallow-to-empty.yml"))
        assert offender not in _naive_astgrep_scope(changed), "naive scope MISSES the untouched offender"
        plan = plan_push_gate(changed, enabled=True)
        assert plan.is_full
        assert ".ast-grep" in plan.reason
        assert plan.astgrep_scope is None, "FULL scans the whole tree, catching the planted offender"


class TestR2NewRuleNewlyViolatesCleanFile:
    def test_manifest_and_rule_add_forces_full(self) -> None:
        changed = _changed(
            ("M", "src/teatree/quality/regression_rules.yaml"),
            ("A", ".ast-grep/blocking/new-rule.yml"),
        )
        assert _naive_astgrep_scope(changed) == [], "naive scope is empty — a previously-clean file is never re-scanned"
        assert plan_push_gate(changed, enabled=True).is_full


class TestR3ConftestChangesDoctestSemantics:
    def test_conftest_forces_full_doctest(self) -> None:
        changed = _changed(("M", "tests/conftest.py"))
        # Naive DOCTEST scope is empty (no src .py changed) — a tree-wide doctest
        # option/fixture change would be silently skipped.
        assert _naive_doctest_scope(changed) == []
        plan = plan_push_gate(changed, enabled=True)
        assert plan.is_full
        assert plan.doctest_targets == (WHOLE_TREE_DOCTEST,)

    def test_pyproject_forces_full(self) -> None:
        plan = plan_push_gate(_changed(("M", "pyproject.toml")), enabled=True)
        assert plan.is_full


class TestR4DoctestLocalityIsSound:
    """The soundness pin: doctest scoping needs no import graph — failures are local."""

    def test_changed_base_module_scopes_doctest_to_itself_only(self) -> None:
        base = "src/teatree/foundation.py"
        consumer = "src/teatree/consumer.py"  # UNCHANGED — its docstring may use base's API
        plan = plan_push_gate(_changed(("M", base)), enabled=True)
        assert not plan.is_full
        assert plan.doctest_targets == (Path(base),)
        # The unchanged consumer's docstring is main's / CI's concern, never the
        # push gate's — that locality is WHY doctest scoping is safe without a graph.
        assert Path(consumer) not in plan.doctest_targets


class TestR5DeleteOrRename:
    def test_delete_forces_full(self) -> None:
        changed = _changed(("D", "src/teatree/foo.py"))
        # A naive selector ignoring status would treat the deleted path as a normal
        # scoped src file; the real classifier forces FULL (its edges are gone).
        assert not plan_push_gate_naive_is_full(changed)
        assert plan_push_gate(changed, enabled=True).is_full

    def test_rename_forces_full(self) -> None:
        assert plan_push_gate(_changed(("R", "src/teatree/renamed.py")), enabled=True).is_full


class TestR6NonPythonDataFile:
    def test_yaml_corpus_under_src_forces_full(self) -> None:
        changed = _changed(("M", "src/teatree/eval/corpus/scenario.yaml"))
        assert _naive_astgrep_scope(changed) == [], "naive scope misses a data-driven yaml a test reads at runtime"
        assert plan_push_gate(changed, enabled=True).is_full

    def test_fixture_under_tests_forces_full(self) -> None:
        assert plan_push_gate(_changed(("M", "tests/fixtures/data.json")), enabled=True).is_full


class TestR7Unclassifiable:
    def test_unknown_path_forces_full(self) -> None:
        changed = _changed(("M", "some/weird/artifact.xyz"))
        assert _naive_astgrep_scope(changed) == []
        plan = plan_push_gate(changed, enabled=True)
        assert plan.is_full
        assert "fail-safe" in plan.reason or "unclassifiable" in plan.reason

    def test_astgrep_engine_absent_defers_loudly_never_wedges(self) -> None:
        # R7 engine-absent: the ast-grep portion is DEFERRED to the CI backstop with
        # a LOUD notice — never silently green, never a wedged push (CI is the guarantor).
        plan = plan_push_gate(_changed(("M", "src/teatree/core/session.py")), enabled=True)

        def _raise(*_args: object, **_kwargs: object) -> list[dict]:
            message = "no engine on PATH"
            raise AstGrepUnavailableError(message)

        result = run_push_gate(
            plan,
            repo_root=Path.cwd(),
            doctest_runner=lambda _t, _r: True,
            astgrep_scanner=_raise,
        )
        assert result.astgrep_deferred is True
        assert result.ok is True, "a missing ast-grep engine must not wedge the push"
        assert any("ast-grep" in note.lower() for note in result.notes)


class TestFlagGatingAndExecutor:
    def test_flag_off_is_always_full_whole_tree(self) -> None:
        # OFF ⇒ whole-tree doctest + whole-tree ast-grep (== today): zero push change.
        plan = plan_push_gate(_changed(("M", "src/teatree/core/session.py")), enabled=False)
        assert plan.is_full
        assert plan.doctest_targets == (WHOLE_TREE_DOCTEST,)
        assert plan.astgrep_scope is None

    def test_flag_on_scopes_a_clean_src_diff(self) -> None:
        plan = plan_push_gate(_changed(("M", "src/teatree/core/session.py")), enabled=True)
        assert not plan.is_full
        assert plan.doctest_targets == (Path("src/teatree/core/session.py"),)
        assert plan.astgrep_scope == (Path("src/teatree/core/session.py"),)

    def test_executor_fails_on_ast_grep_finding(self) -> None:
        plan = plan_push_gate(_changed(("M", "src/teatree/core/session.py")), enabled=True)
        finding = {"check_id": "except-swallow-to-empty", "path": "src/teatree/core/session.py", "start": {"line": 3}}
        result = run_push_gate(
            plan,
            repo_root=Path.cwd(),
            doctest_runner=lambda _t, _r: True,
            astgrep_scanner=lambda *_a, **_k: [finding],
        )
        assert result.ok is False
        assert result.astgrep_findings == (finding,)

    def test_executor_fails_on_doctest_failure(self) -> None:
        plan = plan_push_gate(_changed(("M", "src/teatree/core/session.py")), enabled=True)
        result = run_push_gate(
            plan,
            repo_root=Path.cwd(),
            doctest_runner=lambda _t, _r: False,
            astgrep_scanner=lambda *_a, **_k: [],
        )
        assert result.ok is False


def plan_push_gate_naive_is_full(changed: ChangedSet) -> bool:
    """Naive: FULL only when nothing scopable — a deleted src .py still 'scopes'.

    Models the status-blind selector R5 must beat: it never forces FULL on a
    delete/rename because it treats the (gone) path as a scoped src file.
    """
    return not _naive_astgrep_scope(changed) and not _naive_doctest_scope(changed)


class TestResolvePlanAndDoctestRunner:
    def test_resolve_plan_dirty_merge_base_forces_full(self) -> None:
        with patch.object(push_gate_mod, "changed_paths", side_effect=ChangedSetError("dirty")):
            plan = resolve_plan("origin/main", enabled=True)
        assert plan.is_full
        assert "could not compute" in plan.reason

    def test_resolve_plan_scopes_a_clean_diff(self) -> None:
        changed = _changed(("M", "src/teatree/core/session.py"))
        with patch.object(push_gate_mod, "changed_paths", return_value=changed):
            plan = resolve_plan("origin/main", enabled=True)
        assert not plan.is_full
        assert plan.doctest_targets == (Path("src/teatree/core/session.py"),)

    def test_run_doctests_empty_targets_is_true_without_running(self) -> None:
        with patch.object(push_gate_mod, "run_allowed_to_fail") as run:
            assert _run_doctests((), Path.cwd()) is True
        run.assert_not_called()

    def test_run_doctests_invokes_pytest_doctest_modules(self) -> None:
        with patch.object(push_gate_mod, "run_allowed_to_fail", return_value=SimpleNamespace(returncode=0)) as run:
            ok = _run_doctests((Path("src/teatree/x.py"),), Path.cwd())
        assert ok is True
        cmd = run.call_args.args[0]
        assert "--doctest-modules" in cmd
        assert "src/teatree/x.py" in cmd

    def test_run_doctests_reports_failure_on_nonzero(self) -> None:
        with patch.object(push_gate_mod, "run_allowed_to_fail", return_value=SimpleNamespace(returncode=1)):
            assert _run_doctests((Path("src/teatree/x.py"),), Path.cwd()) is False


class TestReportShape:
    def test_full_and_scoped_reports_are_human_readable(self) -> None:
        full = plan_push_gate(_changed(("M", "pyproject.toml")), enabled=True)
        assert full.report().startswith("push-gate: FULL")
        scoped = plan_push_gate(_changed(("M", "src/teatree/core/session.py")), enabled=True)
        assert scoped.report().startswith("push-gate: SCOPED")


@pytest.mark.parametrize(
    "plan",
    [
        PushGatePlan(is_full=True, reason="r", doctest_targets=(WHOLE_TREE_DOCTEST,), astgrep_scope=None, enabled=True),
        PushGatePlan(
            is_full=False,
            reason="r",
            doctest_targets=(Path("src/teatree/x.py"),),
            astgrep_scope=(Path("src/teatree/x.py"),),
            enabled=True,
        ),
    ],
)
def test_plan_is_frozen(plan: PushGatePlan) -> None:
    with pytest.raises(AttributeError):
        plan.is_full = not plan.is_full  # type: ignore[misc]
