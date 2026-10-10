"""Only a pinned set of call sites may turn a question into an owner DM (#5096).

``DeferredQuestion.record`` is deny-by-default: a row is internal unless the caller passes a
``QuestionCard``, whose ``decision`` names the kind. Each call site that passes one, and each place a
card is built with its kind, is listed here, so a new owner question is a reviewed edit to these lists,
never a side effect of one more ``record(...)``.
"""

import ast
from pathlib import Path

from tests.conformance._src_tree import REPO_ROOT, parsed_modules, src_modules

_DECLARED_BY_THE_CALLER = "<declared by the caller>"

_OWNER_RECORD_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("src/teatree/agents/reactive_envelope_recorders.py", "_maybe_record_answer_draft"),
        ("src/teatree/agents/reactive_envelope_recorders.py", "_maybe_record_triage_recommendations"),
        ("src/teatree/core/gates/directive_interpret_gate.py", "_record_clarifications"),
        ("src/teatree/core/management/commands/questions.py", "record"),
        ("src/teatree/core/management/commands/recipe.py", "_queue_recipe_approval"),
        ("src/teatree/core/models/task_handoff.py", "record_deferred_question"),
        ("src/teatree/core/review/mr_state_question.py", "ask_mr_state"),
        ("src/teatree/loop/scanners/board_reconcile_issue_close.py", "_escalate_unshipped_work_once"),
        ("src/teatree/loop/scanners/inert_gate_questions.py", "scan"),
        ("src/teatree/loop/scanners/override_lift.py", "raise_override_lift_questions"),
        ("src/teatree/loop/scanners/stale_control_db_questions.py", "scan"),
        ("src/teatree/loops/directive_loop/ratify.py", "_reask_question"),
        ("src/teatree/loops/directive_loop/ratify.py", "ask_ratification"),
    }
)

_CARD_KINDS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("src/teatree/agents/reactive_envelope_recorders.py", "<module>", "PUBLIC_POST"),
        ("src/teatree/agents/reactive_envelope_recorders.py", "_maybe_record_triage_recommendations", "PRODUCT_SCOPE"),
        ("src/teatree/core/gates/directive_interpret_gate.py", "<module>", "PRODUCT_SCOPE"),
        ("src/teatree/core/management/commands/questions.py", "record", _DECLARED_BY_THE_CALLER),
        ("src/teatree/core/management/commands/recipe.py", "<module>", "PRODUCT_SCOPE"),
        ("src/teatree/loop/scanners/board_reconcile_issue_close.py", "_escalate_unshipped_work_once", "IRREVERSIBLE"),
        ("src/teatree/loop/scanners/inert_gate_questions.py", "<module>", "ARCHITECTURE"),
        ("src/teatree/loop/scanners/override_lift.py", "_proposal_for", "PRODUCT_SCOPE"),
        ("src/teatree/loop/scanners/review_request_send.py", "_ask", "PUBLIC_POST"),
        ("src/teatree/loop/scanners/stale_control_db_questions.py", "_card", "IRREVERSIBLE"),
        ("src/teatree/loops/directive_loop/ratify.py", "_ratify_card", "ARCHITECTURE"),
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
    return func.attr == name and (name != "record" or ast.unparse(func.value).endswith("DeferredQuestion"))


def _kind(card_call: ast.Call) -> str:
    for keyword in card_call.keywords:
        if keyword.arg == "decision":
            value = keyword.value
            return value.attr if isinstance(value, ast.Attribute) else _DECLARED_BY_THE_CALLER
    return _DECLARED_BY_THE_CALLER


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


def test_no_call_site_names_a_decision_outside_a_card() -> None:
    offenders = [
        (path, function)
        for path, function, call in _calls("record")
        if any(keyword.arg in {"decision", "checked"} for keyword in call.keywords)
    ]
    assert offenders == []


def test_only_the_pinned_call_sites_record_an_owner_card() -> None:
    owner_sites = {
        (path, function) for path, function, call in _calls("record") if any(kw.arg == "card" for kw in call.keywords)
    }
    assert owner_sites == _OWNER_RECORD_SITES


def test_only_the_pinned_places_build_a_card_and_name_its_kind() -> None:
    built = {(path, function, _kind(call)) for path, function, call in _calls("QuestionCard")}
    assert built == _CARD_KINDS


def test_only_the_review_request_sender_asks_the_owner_about_a_merge_request() -> None:
    askers = {
        path for path, _function, call in _calls("ask_mr_state") if any(kw.arg == "owner" for kw in call.keywords)
    }
    assert askers == _OWNER_MR_STATE_ASKERS
