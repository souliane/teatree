# test-path: cross-cutting — drives hooks/scripts/hook_router.py + subagent_hint.py; no src/teatree/ mirror.
"""A sub-agent deny must not advertise a self-authorize escape hatch (#3252).

Public banned-term and quote-scanner refusals keep owner escalation guidance
without naming an override variable. A deny that offers the owner a per-call
``[quote-ok: <reason>]`` approval is rewritten at the ``emit_pretooluse_deny``
chokepoint when a sub-agent receives it; a sub-agent cannot self-authorize a bypass.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.hooks import banned_terms_scanner
from teatree.hooks.quote_gate_messages import format_dispatch_block_message
from teatree.hooks.quote_scanner import HIGH, Finding, ScanResult


@pytest.fixture(autouse=True)
def _breaker_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate the deny-streak state and pin the per-process hook context so the
    # emit chokepoint can read the (main-vs-sub) agent origin.
    monkeypatch.setattr(router, "STATE_DIR", tmp_path / "state")
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")


def _emit_reason(capsys: pytest.CaptureFixture[str], reason: str) -> str:
    assert router.emit_pretooluse_deny(reason) is True
    payload = json.loads(capsys.readouterr().out.strip())
    return payload["hookSpecificOutput"]["permissionDecisionReason"]


class TestSubagentSelfAuthHintSuppression:
    _BANNED_DENY = banned_terms_scanner.format_block_message("acmecorp")

    def test_main_agent_keeps_owner_escalation_without_override_token(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_CURRENT_DATA", {"session_id": "s-main", "tool_name": "Bash"})
        emitted = _emit_reason(capsys, self._BANNED_DENY)
        assert "ALLOW_BANNED_TERM" not in emitted
        assert "ask the owner to review the blocked publication" in emitted.lower()
        assert "re-issue" not in emitted

    def test_subagent_hint_is_rewritten_to_escalation(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            router, "_CURRENT_DATA", {"session_id": "s-sub", "tool_name": "Bash", "agent_id": "agent-7"}
        )
        emitted = _emit_reason(capsys, self._BANNED_DENY)
        # Both agent roles receive owner escalation without an override token.
        assert "ALLOW_BANNED_TERM=1" not in emitted
        assert "ask the owner to review the blocked publication" in emitted.lower()
        # The deny itself is UNCHANGED — still fail-closed, still names the gate.
        assert emitted.startswith("BLOCKED: banned-terms posting gate")
        assert "acmecorp" in emitted


class TestSuppressHelperUnit:
    """Direct unit coverage of the rewrite predicate, over the hint the gates emit today."""

    _HINTED = format_dispatch_block_message(ScanResult([Finding("owner-quote", HIGH, "you said")]))

    def test_main_agent_unchanged(self) -> None:
        assert router._suppress_self_auth_hint_for_subagent(self._HINTED, {}) == self._HINTED

    def test_subagent_does_not_see_the_live_quote_ok_approval(self) -> None:
        out = router._suppress_self_auth_hint_for_subagent(self._HINTED, {"agent_id": "a-1"})

        assert "[quote-ok:" not in out
        assert "A token placed" not in out
        assert "cannot self-authorize" in out
        assert out.startswith("BLOCKED: pre-dispatch quote-scanner gate (#1401).")
        assert "ask the owner" in out

    def test_a_retired_variable_form_is_not_matched(self) -> None:
        reason = (
            "BLOCKED: some gate. Rephrase without the quoted span or ask the owner. "
            "The owner may approve this one post with a per-call QUOTE_OK=1 override (it is recorded)."
        )

        assert router._suppress_self_auth_hint_for_subagent(reason, {"agent_id": "a-1"}) == reason

    def test_subagent_reason_without_hint_is_untouched(self) -> None:
        plain = "BLOCKED: out-of-band merge on a managed repo."
        assert router._suppress_self_auth_hint_for_subagent(plain, {"agent_id": "a-1"}) == plain
