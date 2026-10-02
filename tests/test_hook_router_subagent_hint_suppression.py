# test-path: cross-cutting — drives hooks/scripts/hook_router.py + subagent_hint.py; no src/teatree/ mirror.
"""A sub-agent deny must not advertise a self-authorize escape hatch (#3252).

The banned-terms / quote-scanner leak denies tell the operator to re-issue with a
leading ``ALLOW_BANNED_TERM=1`` / ``QUOTE_OK=1`` env prefix. A SUB-AGENT cannot
self-authorize that bypass: the auto-mode classifier denies the retry as an
"unauthorized safety-gate bypass", which poisoned the sub-agent's whole context.
The router rewrites the hint at the ``emit_pretooluse_deny`` chokepoint when the
call is from a sub-agent (a non-empty ``agent_id``), pointing it at the route it
CAN take — escalate to the main agent / user — while the deny itself stays
fail-closed. A main-agent deny keeps the verbatim escape-hatch hint.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.hooks import banned_terms_scanner


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

    def test_main_agent_keeps_verbatim_escape_hatch_hint(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_CURRENT_DATA", {"session_id": "s-main", "tool_name": "Bash"})
        emitted = _emit_reason(capsys, self._BANNED_DENY)
        assert "ALLOW_BANNED_TERM=1" in emitted
        assert "re-issue the command with a leading" in emitted

    def test_subagent_hint_is_rewritten_to_escalation(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            router, "_CURRENT_DATA", {"session_id": "s-sub", "tool_name": "Bash", "agent_id": "agent-7"}
        )
        emitted = _emit_reason(capsys, self._BANNED_DENY)
        # The escape-hatch hint is gone; the escalation guidance replaces it.
        assert "re-issue the command with a leading" not in emitted
        assert "cannot self-authorize" in emitted
        assert "main agent / user" in emitted
        # The deny itself is UNCHANGED — still fail-closed, still names the gate.
        assert emitted.startswith("BLOCKED: banned-terms posting gate")
        assert "acmecorp" in emitted


class TestSuppressHelperUnit:
    """Direct unit coverage of the rewrite predicate."""

    _HINTED = (
        "BLOCKED: some gate. If the match is a false positive, re-issue the command "
        "with a leading QUOTE_OK=1 env prefix (e.g. `QUOTE_OK=1 <command>`)."
    )

    def test_main_agent_unchanged(self) -> None:
        assert router._suppress_self_auth_hint_for_subagent(self._HINTED, {}) == self._HINTED

    def test_subagent_rewrites(self) -> None:
        out = router._suppress_self_auth_hint_for_subagent(self._HINTED, {"agent_id": "a-1"})
        assert "QUOTE_OK=1 env prefix" not in out
        assert "cannot self-authorize" in out

    def test_subagent_reason_without_hint_is_untouched(self) -> None:
        plain = "BLOCKED: out-of-band merge on a managed repo."
        assert router._suppress_self_auth_hint_for_subagent(plain, {"agent_id": "a-1"}) == plain
