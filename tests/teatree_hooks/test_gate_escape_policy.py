import pytest

from teatree.hooks.gate_escape import EscapePolicy, action_digest, action_excerpt, find_escape, policy_for, strip_escape
from tests._gate_escape_audit import NEVER_ESCAPABLE_GATES


def test_escape_requires_matching_gate_and_reason() -> None:
    payload = {"command": "git status # [gate-escape:main_clone: fixture reason]"}
    assert find_escape(payload, "main_clone") == "fixture reason"
    assert find_escape(payload, "git_add_all") is None
    assert find_escape({"command": "[gate-escape:main_clone:  ]"}, "main_clone") is None


def test_digest_ignores_token_but_keeps_protected_action() -> None:
    original = {"command": "git status"}
    escaped = {"command": "git status # [gate-escape:main_clone: reason]"}
    assert action_digest("Bash", original) == action_digest("Bash", escaped)
    assert action_digest("Bash", {"command": "git push"}) != action_digest("Bash", escaped)
    assert strip_escape(escaped["command"]) == "git status"


@pytest.mark.parametrize("gate", NEVER_ESCAPABLE_GATES)
def test_leak_and_owner_boundary_gates_are_never_escapable(gate: str) -> None:
    assert policy_for(gate) is EscapePolicy.NEVER


def test_policy_preserves_owner_boundaries() -> None:
    assert policy_for("raw_review_post") is EscapePolicy.HUMAN_ONLY
    assert policy_for("main_clone") is EscapePolicy.AUDITED
    assert policy_for("unknown_gate") is EscapePolicy.NEVER


def test_description_metadata_does_not_authorize_an_escape() -> None:
    value = {"command": "git push origin HEAD", "description": "[gate-escape:headless_authoring: fixture]"}
    assert find_escape(value, "headless_authoring") is None


def test_placeholder_reason_from_documentation_does_not_authorize() -> None:
    assert find_escape({"content": "Use `[gate-escape:main_clone: <reason>]` here."}, "main_clone") is None


def test_token_buried_mid_body_does_not_authorize() -> None:
    body = "a" * 600 + " [gate-escape:plan_gate: buried] " + "b" * 600
    assert find_escape({"content": body}, "plan_gate") is None
    assert find_escape({"command": "a" * 2000 + " # [gate-escape:plan_gate: trailing]"}, "plan_gate") == "trailing"


def test_excerpt_is_the_bounded_action_without_the_token() -> None:
    assert action_excerpt({"command": "git   status # [gate-escape:main_clone: reason]"}) == "git status"
    assert len(action_excerpt({"command": "x " * 500})) == 200
    assert action_excerpt({"content": "[gate-escape:plan_gate: reason] body"}) == '{"content": "body"}'
