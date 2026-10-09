"""Each owner-channel provenance has exactly one writer: Slack the reply binder, policy the approval dial."""

import ast

import pytest

from tests.conformance._src_tree import SRC_DIR, src_modules

_STAMP_SITES = {"SLACK": {"loop/question_binding.py"}, "POLICY": {"core/models/approval_dial.py"}}
_STAMP_VALUES = {"slack": "SLACK", "policy": "POLICY"}
_DEFINITION = "core/models/deferred_question.py"


def _is_resolved_via(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "ResolvedVia") or (
        isinstance(node, ast.Name) and node.id == "ResolvedVia"
    )


def _stamp_of(node: ast.AST, reads: set[int]) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr in _STAMP_SITES and id(node) not in reads:
        return node.attr if _is_resolved_via(node.value) else None
    if isinstance(node, ast.keyword) and node.arg == "resolved_via" and isinstance(node.value, ast.Constant):
        return _STAMP_VALUES.get(str(node.value.value))
    return None


def _stamps(tree: ast.Module) -> set[str]:
    reads = {id(node) for compare in ast.walk(tree) if isinstance(compare, ast.Compare) for node in ast.walk(compare)}
    return {stamp for node in ast.walk(tree) if (stamp := _stamp_of(node, reads)) is not None}


def _sites() -> dict[str, set[str]]:
    sites: dict[str, set[str]] = {channel: set() for channel in _STAMP_SITES}
    for path, tree in src_modules():
        module = str(path.relative_to(SRC_DIR))
        if module == _DEFINITION or module.startswith("core/migrations/"):
            continue
        for channel in _stamps(tree):
            sites[channel].add(module)
    return sites


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("q.apply_answer('a', resolved_via=DeferredQuestion.ResolvedVia.SLACK)", {"SLACK"}),
        ("q.apply_answer('a', resolved_via='policy')", {"POLICY"}),
        ("row.resolved_via = ResolvedVia.SLACK", {"SLACK"}),
        ("if row.resolved_via == DeferredQuestion.ResolvedVia.POLICY:\n    pass", set()),
    ],
)
def test_the_scan_tells_a_stamp_from_a_read(source: str, expected: set[str]) -> None:
    assert _stamps(ast.parse(source)) == expected


def test_each_owner_channel_is_stamped_at_its_one_site() -> None:
    assert _sites() == _STAMP_SITES
