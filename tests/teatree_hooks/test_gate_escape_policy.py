from hooks.scripts.gate_ledger import DECLARED_GATES
from teatree.hooks.gate_escape import KNOWN_GATES, EscapePolicy, action_digest, find_escape, policy_for, strip_escape


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


def test_policy_preserves_leak_and_owner_boundaries() -> None:
    assert DECLARED_GATES | {"answer_first", "completion_claim", "unbacked_claim", "standing_goal_stop"} == KNOWN_GATES
    assert policy_for("quote_scanner") is EscapePolicy.NEVER
    assert policy_for("raw_review_post") is EscapePolicy.HUMAN_ONLY
    assert policy_for("main_clone") is EscapePolicy.AUDITED
    assert policy_for("unknown_gate") is EscapePolicy.NEVER


def test_description_metadata_does_not_authorize_an_escape() -> None:
    value = {"command": "git push origin HEAD", "description": "[gate-escape:headless_authoring: fixture]"}
    assert find_escape(value, "headless_authoring") is None
