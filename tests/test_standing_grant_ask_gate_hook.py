# test-path: cross-cutting — tests hooks/scripts/standing_grant_ask_gate.py (hooks/); no src/teatree/ mirror.
"""PreToolUse gate: never ask the owner to sign off a substrate merge a standing grant holds (#2663).

The decision core is pinned separately; this covers the hook's contract against
real ``ConfigSetting`` rows — the grant lookup by repo identity, the never-lockout
escapes, and the chain position that keeps a loop-driven ask out of the
``DeferredQuestion`` mirror.
"""

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import standing_grant_ask_gate
from hooks.scripts.hook_router import handle_block_standing_grant_ask
from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

OVERLAY = "t3-teatree"
SLUG = "souliane/teatree"
OWNER = "owner:standing"
_COVERED_ASK = (
    f"Do you approve merging substrate PR https://github.com/{SLUG}/pull/4892 with --human-authorize {OWNER}?"
)
_WAIVER_ASK = "Authorize the expedite waiver for substrate PR #4805 so it can merge on pending checks?"


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: needs the app registry

    monkeypatch.setattr(standing_grant_ask_gate, "_standing_grant_ask_gate_enabled", lambda: True)
    monkeypatch.setenv("T3_OVERLAY_NAME", "")
    overlay = get_all_overlays()[OVERLAY]
    with patch.object(type(overlay), "get_workspace_repos", return_value=[SLUG]):
        yield


@contextmanager
def _config(**settings: object) -> Iterator[None]:
    rows = [ConfigSetting.objects.set_value(key, value, scope=OVERLAY) for key, value in settings.items()]
    try:
        yield
    finally:
        for row in rows:
            row.delete()


def _grant_on() -> AbstractContextManager[None]:
    return _config(autonomy="full", substrate_self_signoff=True)


def _ask(*questions: str, session_id: str = "sess-grant-ask") -> dict:
    return {
        "session_id": session_id,
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {"question": q, "header": "Merge", "options": [{"label": "Yes"}, {"label": "No"}]} for q in questions
            ]
        },
    }


def _deny_reason(capsys: pytest.CaptureFixture[str]) -> str:
    output = capsys.readouterr().out.strip()
    return json.loads(output)["permissionDecisionReason"] if output else ""


class TestDenies:
    def test_a_grant_covered_ask_is_denied_naming_the_overlay_and_the_grant(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is True

        reason = _deny_reason(capsys)
        assert OVERLAY in reason
        assert "substrate_self_signoff" in reason

    def test_the_configured_delegation_also_holds_the_sign_off(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _config(autonomy="full", substrate_auto_merge_authorized_by=OWNER):
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is True

        assert f"--human-authorize {OWNER}" in _deny_reason(capsys)

    def test_repo_identity_beats_the_session_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-some-other-overlay")

        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is True

    def test_a_question_naming_no_repo_uses_the_session_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", OVERLAY)

        with _grant_on():
            assert handle_block_standing_grant_ask(_ask("OK to land the substrate PR?")) is True


class TestAllows:
    def test_with_no_standing_grant_asking_is_correct(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _config(autonomy="full"):
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is False

        assert _deny_reason(capsys) == ""

    def test_self_signoff_below_full_autonomy_is_no_grant(self) -> None:
        with _config(autonomy="notify", substrate_self_signoff=True):
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is False

    def test_an_ask_the_grant_does_not_cover_passes(self) -> None:
        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(_WAIVER_ASK)) is False

    def test_other_tools_are_out_of_scope(self) -> None:
        event = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": f"echo '{_COVERED_ASK}'"}}

        with _grant_on():
            assert handle_block_standing_grant_ask(event) is False

    def test_malformed_tool_input_passes(self) -> None:
        with _grant_on():
            assert handle_block_standing_grant_ask({"tool_name": "AskUserQuestion", "tool_input": []}) is False


class TestNeverLockout:
    def test_kill_switch_disables_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(standing_grant_ask_gate, "_standing_grant_ask_gate_enabled", lambda: False)

        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is False

    def test_per_call_token_allows_and_notes(self, capsys: pytest.CaptureFixture[str]) -> None:
        question = f"[grant-ask-ok: the owner asked to review this one personally] {_COVERED_ASK}"

        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(question)) is False

        assert "grant-ask-ok" in capsys.readouterr().err

    def test_an_empty_token_reason_does_not_escape(self) -> None:
        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(f"[grant-ask-ok: ] {_COVERED_ASK}")) is True

    def test_cold_env_without_the_core_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(standing_grant_ask_gate, "_load_core", lambda: None)

        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is False

    def test_an_unreadable_grant_fails_open_and_says_skipped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(standing_grant_ask_gate, "bootstrap_teatree_django", lambda: False)

        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is False

        assert "SKIPPED" in capsys.readouterr().err

    def test_deny_routes_through_the_fail_open_chokepoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr(router, "_danger_gate_fail_open_enabled", lambda: True)
        monkeypatch.setattr(router, "emit_pretooluse_deny", lambda reason, **_: seen.append(reason) or True)

        with _grant_on():
            assert handle_block_standing_grant_ask(_ask(_COVERED_ASK)) is False
        assert seen == []


class TestChainPosition:
    """A loop-driven ask is refused BEFORE the mirror would defer it to the owner's Slack."""

    def _run_pretooluse_chain(self, data: dict) -> None:
        for handler in router._HANDLERS["PreToolUse"]:
            if handler(data):
                return

    def _loop_driven(self) -> AbstractContextManager[None]:
        @contextmanager
        def _patched() -> Iterator[None]:
            with (
                patch.object(router, "_is_live_user_turn", return_value=False),
                patch.object(router, "_session_drives_loop", return_value=True),
                patch.object(router, "_kick_question_drain"),
            ):
                yield

        return _patched()

    def test_a_grant_covered_loop_ask_never_reaches_the_owner(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _grant_on(), self._loop_driven():
            self._run_pretooluse_chain(_ask(_COVERED_ASK, session_id="sess-loop-grant"))

        assert "substrate_self_signoff" in _deny_reason(capsys)
        assert not DeferredQuestion.objects.filter(session_id="sess-loop-grant").exists()

    def test_without_the_grant_the_same_loop_ask_is_deferred_to_the_owner(self) -> None:
        with _config(autonomy="full"), self._loop_driven():
            self._run_pretooluse_chain(_ask(_COVERED_ASK, session_id="sess-loop-nogrant"))

        assert DeferredQuestion.objects.filter(session_id="sess-loop-nogrant").count() == 1
