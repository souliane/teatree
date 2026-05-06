"""Tests for ``teatree.loop.dispatch`` — signal → action routing."""

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
