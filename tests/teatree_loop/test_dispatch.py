"""Tests for ``teatree.loop.dispatch`` — signal → action routing."""

import pytest

from teatree.config import UserSettings
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal


def test_my_pr_failed_routes_to_action_needed_statusline() -> None:
    actions = dispatch([ScanSignal(kind="my_pr.failed", summary="PR #1 failed")])
    assert len(actions) == 1
    assert actions[0].kind == "statusline"
    assert actions[0].zone == "action_needed"


def test_my_pr_open_routes_to_in_flight_statusline() -> None:
    actions = dispatch([ScanSignal(kind="my_pr.open", summary="PR #1 open")])
    assert actions[0].kind == "statusline"
    assert actions[0].zone == "in_flight"


def test_reviewer_pr_new_sha_dispatches_to_reviewer_agent() -> None:
    actions = dispatch([ScanSignal(kind="reviewer_pr.new_sha", summary="MR x")])
    assert actions[0].kind == "agent"
    assert actions[0].zone == "t3:reviewer"


def test_reviewer_pr_approval_dismissed_dispatches_to_reviewer_agent() -> None:
    actions = dispatch([ScanSignal(kind="reviewer_pr.approval_dismissed", summary="MR x")])
    kinds = [(a.kind, a.zone) for a in actions]
    # Dual dispatch: agent + statusline mirror in action_needed.
    assert ("agent", "t3:reviewer") in kinds
    assert ("statusline", "action_needed") in kinds


def test_pending_task_dispatches_to_orchestrator() -> None:
    actions = dispatch([ScanSignal(kind="pending_task", summary="Task 1 pending")])
    assert actions[0].kind == "agent"
    assert actions[0].zone == "t3:orchestrator"


def test_notion_unrouted_routes_to_webhook() -> None:
    actions = dispatch([ScanSignal(kind="notion.unrouted", summary="Item to route")])
    assert actions[0].kind == "webhook"
    assert actions[0].zone == "n8n"


def test_unknown_kind_falls_back_to_in_flight() -> None:
    actions = dispatch([ScanSignal(kind="custom.signal", summary="Custom")])
    assert actions[0].kind == "statusline"
    assert actions[0].zone == "in_flight"


def test_payload_propagates_through_dispatch() -> None:
    payload: dict[str, object] = {"url": "https://example.com/mr/1"}
    actions = dispatch([ScanSignal(kind="reviewer_pr.new_sha", summary="x", payload=payload)])
    assert actions[0].payload == payload


def test_slack_mention_with_pr_url_emits_review_request_agent() -> None:
    payload: dict[str, object] = {
        "event": {"text": "please review https://gitlab.com/group/proj/-/merge_requests/42", "ts": "1.0"},
    }
    actions = dispatch([ScanSignal(kind="slack.mention", summary="mention", payload=payload)])
    kinds = [(a.kind, a.zone) for a in actions]
    assert ("agent", "t3:reviewer") in kinds
    review_action = next(a for a in actions if a.zone == "t3:reviewer")
    assert review_action.payload["url"] == "https://gitlab.com/group/proj/-/merge_requests/42"


def test_slack_dm_without_pr_url_only_emits_statusline() -> None:
    payload: dict[str, object] = {"event": {"text": "just a chat", "ts": "2.0"}}
    actions = dispatch([ScanSignal(kind="slack.dm", summary="dm", payload=payload)])
    assert [a.kind for a in actions] == ["statusline"]
    assert actions[0].zone == "action_needed"


def test_slack_mention_with_github_pr_url_routes_to_reviewer() -> None:
    payload: dict[str, object] = {"event": {"text": "look https://github.com/o/r/pull/9", "ts": "3.0"}}
    actions = dispatch([ScanSignal(kind="slack.mention", summary="mention", payload=payload)])
    review_actions = [a for a in actions if a.zone == "t3:reviewer"]
    assert len(review_actions) == 1
    assert review_actions[0].payload["url"] == "https://github.com/o/r/pull/9"


def test_slack_signal_without_event_dict_emits_only_statusline() -> None:
    actions = dispatch([ScanSignal(kind="slack.mention", summary="x", payload={"event": "not-a-dict"})])
    assert [a.kind for a in actions] == ["statusline"]


def test_slack_signal_with_non_string_text_emits_only_statusline() -> None:
    payload: dict[str, object] = {"event": {"text": 42}}
    actions = dispatch([ScanSignal(kind="slack.dm", summary="x", payload=payload)])
    assert [a.kind for a in actions] == ["statusline"]


def test_assigned_issue_ready_with_auto_start_dispatches_to_orchestrator() -> None:
    actions = dispatch([ScanSignal(kind="assigned_issue.ready", summary="Issue 5", payload={"auto_start": True})])
    assert actions[0].kind == "agent"
    assert actions[0].zone == "t3:orchestrator"


def test_assigned_issue_ready_without_auto_start_goes_to_statusline() -> None:
    actions = dispatch([ScanSignal(kind="assigned_issue.ready", summary="Issue 5", payload={"auto_start": False})])
    assert actions[0].kind == "statusline"
    assert actions[0].zone == "action_needed"


def test_assigned_issue_ready_default_payload_goes_to_statusline() -> None:
    actions = dispatch([ScanSignal(kind="assigned_issue.ready", summary="Issue 5")])
    assert actions[0].kind == "statusline"
    assert actions[0].zone == "action_needed"


def _answering_signal(extra: dict[str, object] | None = None) -> ScanSignal:
    payload: dict[str, object] = {"event_id": 7, "phase": "answering", "target_ref": "slack:C1"}
    if extra:
        payload.update(extra)
    return ScanSignal(
        kind="incoming_event.task_needed",
        summary="task request from slack (answering): what's the status of !42?",
        payload=payload,
    )


def _pin_settings(monkeypatch: pytest.MonkeyPatch, settings: UserSettings) -> None:
    """Pin the dispatcher's effective settings (no toml/overlay resolution)."""

    def _resolve() -> UserSettings:
        return settings

    monkeypatch.setattr("teatree.loop.dispatch.get_effective_settings", _resolve)


@pytest.fixture
def default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin effective settings to dataclass defaults (no toml, no overlay)."""
    _pin_settings(monkeypatch, UserSettings())


@pytest.mark.usefixtures("default_settings")
def test_answering_task_dispatches_to_answerer_agent() -> None:
    actions = dispatch([_answering_signal()])
    kinds = [(a.kind, a.zone) for a in actions]
    # Dual dispatch: t3:answerer agent + statusline mirror, mirroring the
    # reviewer pattern so the user sees the pending answer before the agent.
    assert ("agent", "t3:answerer") in kinds
    assert ("statusline", "action_needed") in kinds


@pytest.mark.usefixtures("default_settings")
def test_answering_task_does_not_route_to_orchestrator_or_reviewer() -> None:
    actions = dispatch([_answering_signal()])
    agent_zones = {a.zone for a in actions if a.kind == "agent"}
    assert agent_zones == {"t3:answerer"}


@pytest.mark.usefixtures("default_settings")
def test_coding_task_still_only_statusline() -> None:
    """`coding`-phase task_needed keeps its pre-#670 statusline-only behaviour."""
    signal = ScanSignal(
        kind="incoming_event.task_needed",
        summary="task request from slack (coding): implement the dashboard",
        payload={"event_id": 8, "phase": "coding", "target_ref": "slack:C2"},
    )
    actions = dispatch([signal])
    assert [a.kind for a in actions] == ["statusline"]
    assert actions[0].zone == "action_needed"


@pytest.mark.usefixtures("default_settings")
def test_answering_task_default_payload_requires_approval() -> None:
    """Default setting (require_human_approval_to_answer=True) → draft path."""
    actions = dispatch([_answering_signal()])
    agent = next(a for a in actions if a.kind == "agent")
    assert agent.payload["require_human_approval_to_answer"] is True


def test_answering_task_honors_disabled_approval_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting flipped off (per-overlay/global) → direct-post path in payload."""
    _pin_settings(monkeypatch, UserSettings(require_human_approval_to_answer=False))
    actions = dispatch([_answering_signal()])
    agent = next(a for a in actions if a.kind == "agent")
    assert agent.payload["require_human_approval_to_answer"] is False


@pytest.mark.usefixtures("default_settings")
def test_answering_task_normalizes_phase_alias() -> None:
    """Phase vocabulary is normalized (#694) before routing to the answerer."""
    actions = dispatch([_answering_signal({"phase": "  Answering "})])
    agent_zones = {a.zone for a in actions if a.kind == "agent"}
    assert agent_zones == {"t3:answerer"}


@pytest.mark.usefixtures("default_settings")
def test_answering_task_preserves_original_payload_keys() -> None:
    actions = dispatch([_answering_signal()])
    agent = next(a for a in actions if a.kind == "agent")
    assert agent.payload["event_id"] == 7
    assert agent.payload["target_ref"] == "slack:C1"
    assert agent.payload["phase"] == "answering"
