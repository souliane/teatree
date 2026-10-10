"""Only a pinned set of call sites may turn a question into an owner DM (#5096).

``DeferredQuestion.record`` is deny-by-default: a row is internal unless the caller names an
``OwnerDecision``. Each call site that names one is listed here with its kind, so a new owner
question is a reviewed edit to this list, never a side effect of one more ``record(...)``.
"""

import ast
from pathlib import Path

from tests.conformance._src_tree import REPO_ROOT, parsed_modules, src_modules

_DECLARED_BY_THE_CALLER = "<declared by the caller>"

_OWNER_CALL_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("src/teatree/agents/reactive_envelope_recorders.py", "_maybe_record_answer_draft", "PUBLIC_POST"),
        ("src/teatree/agents/reactive_envelope_recorders.py", "_maybe_record_triage_recommendations", "PRODUCT_SCOPE"),
        ("src/teatree/core/gates/directive_interpret_gate.py", "_record_clarifications", "PRODUCT_SCOPE"),
        ("src/teatree/core/management/commands/questions.py", "record", _DECLARED_BY_THE_CALLER),
        ("src/teatree/core/management/commands/recipe.py", "_queue_recipe_approval", "PRODUCT_SCOPE"),
        ("src/teatree/core/models/task_handoff.py", "record_deferred_question", _DECLARED_BY_THE_CALLER),
        ("src/teatree/core/review/mr_state_question.py", "ask_mr_state", _DECLARED_BY_THE_CALLER),
        ("src/teatree/loop/scanners/board_reconcile_issue_close.py", "_escalate_unshipped_work_once", "IRREVERSIBLE"),
        ("src/teatree/loop/scanners/inert_gate_questions.py", "scan", "ARCHITECTURE"),
        ("src/teatree/loop/scanners/override_lift.py", "raise_override_lift_questions", "PRODUCT_SCOPE"),
        ("src/teatree/loop/scanners/stale_control_db_questions.py", "scan", "IRREVERSIBLE"),
        ("src/teatree/loops/directive_loop/ratify.py", "_reask_question", "ARCHITECTURE"),
        ("src/teatree/loops/directive_loop/ratify.py", "ask_ratification", "ARCHITECTURE"),
    }
)

#: The one ``ask_mr_state`` caller asking the owner; every other merge-request question is the factory's.
_OWNER_MR_STATE_ASKERS = frozenset({"src/teatree/loop/scanners/review_request_send.py"})

#: Fewer calls than these means the walk stopped seeing them, not that they went away.
_MIN_RECORD_CALLS = 30
_MIN_MR_STATE_ASKS = 4


def _calls(name: str) -> list[tuple[str, str, ast.Call]]:
    modules = (*src_modules(), *parsed_modules(REPO_ROOT / "hooks"))
    return [call for path, tree in modules for call in _calls_in(path, tree, name)]


def _calls_in(path: Path, tree: ast.Module, name: str) -> list[tuple[str, str, ast.Call]]:
    owner = {
        child: function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        for child in ast.walk(function)
    }
    return [
        (str(path.relative_to(REPO_ROOT)), owner.get(node, "<module>"), node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_call_to(node, name)
    ]


def _is_call_to(call: ast.Call, name: str) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == name
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr == name and ast.unparse(func.value).endswith("DeferredQuestion")


def _kind(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "decision":
            value = keyword.value
            return value.attr if isinstance(value, ast.Attribute) else _DECLARED_BY_THE_CALLER
    return None


def test_the_walk_sees_every_question_producer() -> None:
    assert len(_calls("record")) >= _MIN_RECORD_CALLS
    assert len(_calls("ask_mr_state")) >= _MIN_MR_STATE_ASKS


def test_no_call_site_sets_the_audience_directly() -> None:
    offenders = [
        (path, function)
        for path, function, call in _calls("record")
        if any(keyword.arg == "audience" for keyword in call.keywords)
    ]
    assert offenders == []


def test_only_the_pinned_call_sites_name_an_owner_decision() -> None:
    owner_sites = {
        (path, function, kind) for path, function, call in _calls("record") if (kind := _kind(call)) is not None
    }
    assert owner_sites == _OWNER_CALL_SITES


def test_only_the_review_request_sender_asks_the_owner_about_a_merge_request() -> None:
    askers = {
        path for path, _function, call in _calls("ask_mr_state") if any(kw.arg == "owner" for kw in call.keywords)
    }
    assert askers == _OWNER_MR_STATE_ASKERS
