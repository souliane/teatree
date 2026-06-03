"""Tests for ``teatree.loop.dispatch`` — signal → action routing."""

import logging

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.loop import dispatch as dispatch_module
from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.scanners.base import ScanSignal


class MyPrFailedDispatchTests(TestCase):
    def test_my_pr_failed_dispatches_to_debug_agent_and_statusline(self) -> None:
        """``my_pr.failed`` dispatches to ``t3:debug`` and mirrors the statusline (#1295 cap D)."""
        actions = dispatch(
            [
                ScanSignal(
                    kind="my_pr.failed",
                    summary="PR #1 failed",
                    payload={"pr_url": "https://example.com/pr/1", "head_sha": "abc12345"},
                ),
            ],
        )
        kinds_zones = [(a.kind, a.zone) for a in actions]
        assert ("agent", "t3:debug") in kinds_zones
        assert ("statusline", "action_needed") in kinds_zones

    def test_my_pr_failed_idempotent_on_same_head_sha(self) -> None:
        """Re-dispatching on the same ``(pr_url, head_sha)`` yields no second agent action (#1295 cap D)."""
        signal = ScanSignal(
            kind="my_pr.failed",
            summary="PR #1 failed",
            payload={"pr_url": "https://example.com/pr/42", "head_sha": "deadbeef00"},
        )
        first = dispatch([signal])
        second = dispatch([signal])
        first_agents = [a for a in first if a.kind == "agent"]
        second_agents = [a for a in second if a.kind == "agent"]
        assert len(first_agents) == 1
        assert second_agents == []
        # Statusline mirror still fires on every tick — user sees the
        # red PR even though the agent does not re-run.
        assert any(a.kind == "statusline" for a in second)


def test_my_pr_open_routes_to_in_flight_statusline() -> None:
    actions = dispatch([ScanSignal(kind="my_pr.open", summary="PR #1 open")])
    assert actions[0].kind == "statusline"
    assert actions[0].zone == "in_flight"


def test_reviewer_pr_new_sha_dispatches_to_reviewer_agent() -> None:
    actions = dispatch([ScanSignal(kind="reviewer_pr.new_sha", summary="MR x")])
    assert actions[0].kind == "agent"
    assert actions[0].zone == "t3:reviewer"


def test_issue_implementer_claimed_dispatches_to_orchestrator_and_statusline() -> None:
    """A claimed auto-implement issue is a maker-side kickoff to ``t3:orchestrator`` (#1554)."""
    actions = dispatch(
        [
            ScanSignal(
                kind="issue_implementer.claimed",
                summary="Claimed for auto-implement: do it",
                payload={"url": "https://github.com/souliane/teatree/issues/100"},
            ),
        ],
    )
    kinds_zones = [(a.kind, a.zone) for a in actions]
    assert ("agent", "t3:orchestrator") in kinds_zones
    assert ("statusline", "action_needed") in kinds_zones
    # Payload carries the issue URL through to the dispatched agent.
    agent = next(a for a in actions if a.kind == "agent")
    assert agent.payload["url"] == "https://github.com/souliane/teatree/issues/100"


def test_reviewer_pr_approval_dismissed_dispatches_to_reviewer_agent() -> None:
    actions = dispatch([ScanSignal(kind="reviewer_pr.approval_dismissed", summary="MR x")])
    kinds = [(a.kind, a.zone) for a in actions]
    # Dual dispatch: agent + statusline mirror in action_needed.
    assert ("agent", "t3:reviewer") in kinds
    assert ("statusline", "action_needed") in kinds


def test_pending_task_dispatches_to_phase_agent() -> None:
    """A ``pending_task`` routes to its PHASE's own agent, never a chaining orchestrator."""
    actions = dispatch(
        [
            ScanSignal(
                kind="pending_task",
                summary="Task 1 (coding) pending",
                payload={"task_id": 1, "phase": "coding", "ticket_id": 1, "ticket_role": "author"},
            ),
        ],
    )
    assert actions[0].kind == "agent"
    assert actions[0].zone == "t3:coder"


def test_pending_task_shipping_dispatches_to_shipper() -> None:
    actions = dispatch(
        [
            ScanSignal(
                kind="pending_task",
                summary="Task 9 (shipping) pending",
                payload={"task_id": 9, "phase": "shipping", "ticket_id": 3, "ticket_role": "author"},
            ),
        ],
    )
    assert [(a.kind, a.zone) for a in actions] == [("agent", "t3:shipper")]


def test_pending_task_unregistered_phase_falls_through_to_statusline() -> None:
    """A pending task with no registered phase agent surfaces for operator triage."""
    actions = dispatch(
        [
            ScanSignal(
                kind="pending_task",
                summary="Task 2 (scoping) pending",
                payload={"task_id": 2, "phase": "scoping", "ticket_id": 1, "ticket_role": "author"},
            ),
        ],
    )
    assert [(a.kind, a.zone) for a in actions] == [("statusline", "in_flight")]


def test_notion_unrouted_routes_to_webhook() -> None:
    actions = dispatch([ScanSignal(kind="notion.unrouted", summary="Item to route")])
    assert actions[0].kind == "webhook"
    assert actions[0].zone == "n8n"


def test_red_card_signal_dual_dispatches_to_orchestrator_and_statusline() -> None:
    """#1130: a user RED CARD signal routes to the orchestrator (corrective-action workflow) + statusline mirror."""
    payload: dict[str, object] = {
        "row_id": 7,
        "signal_kind": "red_circle",
        "user_id": "U0A72P7CK0A",
        "channel": "C09",
        "ts": "1779180558.938799",
    }
    actions = dispatch([ScanSignal(kind="red_card.signal", summary="RED CARD (red_circle) from U...", payload=payload)])
    kinds = [(a.kind, a.zone) for a in actions]
    assert ("agent", "t3:orchestrator") in kinds
    assert ("statusline", "action_needed") in kinds
    agent_action = next(a for a in actions if a.kind == "agent")
    assert agent_action.payload == payload


def test_slack_review_intent_dual_dispatches_to_reviewer_and_statusline() -> None:
    """#1047: reaction-driven review intent routes to the reviewer agent + statusline mirror."""
    payload: dict[str, object] = {
        "url": "https://gitlab.com/group/proj/-/merge_requests/42",
        "mr_url": "https://gitlab.com/group/proj/-/merge_requests/42",
        "trigger": "reaction",
    }
    actions = dispatch([ScanSignal(kind="slack.review_intent", summary="intent", payload=payload)])
    kinds = [(a.kind, a.zone) for a in actions]
    assert ("agent", "t3:reviewer") in kinds
    assert ("statusline", "action_needed") in kinds


def test_slack_review_intent_payload_propagates() -> None:
    payload: dict[str, object] = {
        "url": "https://gitlab.com/group/proj/-/merge_requests/42",
        "trigger": "mention",
        "user_id": "U0A72P7CK0A",
    }
    actions = dispatch([ScanSignal(kind="slack.review_intent", summary="intent", payload=payload)])
    agent_action = next(a for a in actions if a.kind == "agent")
    assert agent_action.payload == payload


def test_reviewer_pr_task_orphaned_routes_to_mechanical_handler() -> None:
    """#998: scanner-emitted orphan signal dispatches to the cleanup handler."""
    payload: dict[str, object] = {
        "url": "https://gitlab/x/-/merge_requests/373",
        "ticket_id": 42,
    }
    actions = dispatch([ScanSignal(kind="reviewer_pr.task_orphaned", summary="orphan", payload=payload)])
    assert len(actions) == 1
    assert actions[0].kind == "mechanical"
    assert actions[0].zone == "reviewer_task_orphaned"
    assert actions[0].payload == payload


def test_unknown_kind_falls_back_to_in_flight() -> None:
    actions = dispatch([ScanSignal(kind="custom.signal", summary="Custom")])
    assert actions[0].kind == "statusline"
    assert actions[0].zone == "in_flight"


def test_outbound_audit_skipped_does_not_render_to_statusline() -> None:
    """``outbound.audit_skipped`` is a credential-gap diagnostic, not user-actionable.

    Without the drop, every unverifiable claim renders one statusline row per
    tick — at scale, the in_flight zone fills with N copies of "No verifier
    for <kind> overlay=<overlay>" and crowds out real signal (#1372).
    """
    signal = ScanSignal(
        kind="outbound.audit_skipped",
        summary="No verifier for slack_dm overlay=<default>",
        payload={"claim_id": 1, "claim_kind": "slack_dm", "overlay": ""},
    )
    actions = dispatch([signal])
    assert actions == []


class SelfUpdateStatuslineTests(TestCase):
    """#1760: a CI-green-gated self-update skip surfaces; the rest stays noise."""

    def _zone(self, kind: str, reason: str) -> str | None:
        signal = ScanSignal(
            kind=kind,
            summary=f"self-update teatree: {reason}",
            payload={"repo": "teatree", "outcome": kind.split(".", 1)[-1], "reason": reason},
        )
        actions = dispatch([signal])
        return actions[0].zone if actions else None

    def test_ci_red_skip_surfaces_in_action_needed(self) -> None:
        assert self._zone("self_update.skipped", "ci_red") == "action_needed"

    def test_ci_pending_skip_surfaces_in_action_needed(self) -> None:
        assert self._zone("self_update.skipped", "ci_pending") == "action_needed"

    def test_ci_unknown_skip_surfaces_in_action_needed(self) -> None:
        assert self._zone("self_update.skipped", "ci_unknown") == "action_needed"

    def test_dirty_tree_skip_is_dropped(self) -> None:
        # A non-CI skip (dirty tracked tree) is diagnostic-only noise.
        assert self._zone("self_update.skipped", "dirty_tracked:foo.py") is None

    def test_branch_mismatch_skip_is_dropped(self) -> None:
        assert self._zone("self_update.skipped", "branch=feat!=main") is None

    def test_up_to_date_is_dropped(self) -> None:
        assert self._zone("self_update.up_to_date", "") is None

    def test_cadence_not_elapsed_is_dropped(self) -> None:
        assert self._zone("self_update.cadence_not_elapsed", "recent_marker") is None

    def test_updated_is_dropped(self) -> None:
        assert self._zone("self_update.updated", "") is None

    def test_failed_is_dropped(self) -> None:
        assert self._zone("self_update.failed", "fetch:boom") is None


class PrSweepFlagStatuslineTests(TestCase):
    """#68/#78: pr_sweep flag-level signals surface; the rest of the family is dropped."""

    def _action(self, kind: str, reason: str) -> DispatchAction | None:
        signal = ScanSignal(
            kind=kind,
            summary=f"souliane/teatree#42 {reason}",
            payload={"slug": "souliane/teatree", "pr_id": 42, "reason": reason, "merged": False},
        )
        actions = dispatch([signal])
        return actions[0] if actions else None

    def test_conflict_flag_surfaces_in_action_needed(self) -> None:
        action = self._action("pr_sweep.flag_conflict", "conflict")
        assert action is not None
        assert action.kind == "statusline"
        assert action.zone == "action_needed"

    def test_no_review_flag_surfaces_in_action_needed(self) -> None:
        action = self._action("pr_sweep.flag_no_review", "solo_overlay_no_review")
        assert action is not None
        assert action.kind == "statusline"
        assert action.zone == "action_needed"

    def test_diagnostic_pr_sweep_outcomes_still_dropped(self) -> None:
        # Anti-vacuous: the exemption is flag-only — a normal merged/skip
        # outcome stays diagnostic noise off the statusline.
        assert self._action("pr_sweep.merged", "all_green") is None
        assert self._action("pr_sweep.skip", "no_clear_for_head") is None


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


# --- Slack-ping → auto-review bridge (#219) ----------------------------------
#
# A Slack review request ("can you review MR X") arriving via the webhook
# path (`/hooks/slack/` → IncomingEvent → classifier → router → scanner)
# becomes an ``incoming_event.task_needed`` signal with phase ``coding``.
# Before the bridge it fell through to a passive statusline note and the
# referenced PR was never independently reviewed. The bridge mirrors the
# existing ``slack.mention``/``slack.dm`` → ``t3:reviewer`` path: when the
# task detail carries a PR/MR URL it dual-dispatches to the reviewer agent.


def _review_request_signal(detail: str, *, phase: str = "coding") -> ScanSignal:
    return ScanSignal(
        kind="incoming_event.task_needed",
        summary=f"task request from slack ({phase}): {detail}",
        payload={"event_id": 9, "phase": phase, "target_ref": "slack:C9", "detail": detail},
    )


@pytest.mark.usefixtures("default_settings")
def test_incoming_task_with_gitlab_mr_url_dispatches_to_reviewer() -> None:
    signal = _review_request_signal("can you review https://gitlab.com/g/p/-/merge_requests/42")
    actions = dispatch([signal])
    kinds = [(a.kind, a.zone) for a in actions]
    assert ("agent", "t3:reviewer") in kinds
    assert ("statusline", "action_needed") in kinds
    review_action = next(a for a in actions if a.zone == "t3:reviewer")
    assert review_action.payload["url"] == "https://gitlab.com/g/p/-/merge_requests/42"
    assert review_action.payload["event_id"] == 9


@pytest.mark.usefixtures("default_settings")
def test_incoming_task_with_github_pr_url_dispatches_to_reviewer() -> None:
    signal = _review_request_signal("please review https://github.com/o/r/pull/7")
    actions = dispatch([signal])
    review_actions = [a for a in actions if a.zone == "t3:reviewer"]
    assert len(review_actions) == 1
    assert review_actions[0].kind == "agent"
    assert review_actions[0].payload["url"] == "https://github.com/o/r/pull/7"


@pytest.mark.usefixtures("default_settings")
def test_incoming_task_url_found_in_summary_when_detail_absent() -> None:
    """The URL is extracted from the summary if no ``detail`` key is present."""
    signal = ScanSignal(
        kind="incoming_event.task_needed",
        summary="task request from slack (coding): review https://github.com/o/r/pull/3",
        payload={"event_id": 11, "phase": "coding", "target_ref": "slack:C9"},
    )
    actions = dispatch([signal])
    review_actions = [a for a in actions if a.zone == "t3:reviewer"]
    assert len(review_actions) == 1
    assert review_actions[0].payload["url"] == "https://github.com/o/r/pull/3"


@pytest.mark.usefixtures("default_settings")
def test_incoming_task_without_pr_url_keeps_statusline_only() -> None:
    """A coding task with no PR URL keeps the pre-bridge statusline behaviour."""
    signal = _review_request_signal("implement the new dashboard widget")
    actions = dispatch([signal])
    assert [a.kind for a in actions] == ["statusline"]
    assert actions[0].zone == "action_needed"


@pytest.mark.usefixtures("default_settings")
def test_incoming_answering_task_with_pr_url_still_routes_to_reviewer() -> None:
    """A review request is a review request regardless of classified phase."""
    signal = _review_request_signal(
        "can you review https://gitlab.com/g/p/-/merge_requests/8",
        phase="answering",
    )
    actions = dispatch([signal])
    agent_zones = {a.zone for a in actions if a.kind == "agent"}
    assert agent_zones == {"t3:reviewer"}


def _slack_user_reply_signal() -> ScanSignal:
    """Mirror what ``SlackDmInboundScanner`` emits for a drained user reply."""
    return ScanSignal(
        kind="slack.user_reply",
        summary="Slack user reply 1779215938.999779: if there are posted in the channel",
        payload={
            "ts": "1779215938.999779",
            "channel": "C9XYZ",
            "user_id": "U123",
            "text": "if there are posted in the channel",
            "overlay": "t3-teatree",
        },
    )


def test_slack_user_reply_does_not_emit_statusline_action() -> None:
    """#1113 Defect 2: the reactive Slack-answer loop owns replies.

    ``slack.user_reply`` must not fall through to the statusline-action
    fallback — the raw user text/ts is not an operator action item.
    """
    actions = dispatch([_slack_user_reply_signal()])
    assert not any(a.kind == "statusline" for a in actions), [(a.kind, a.zone, a.detail) for a in actions]


def test_slack_user_reply_routes_only_to_its_real_consumer() -> None:
    """Routed mechanically (the drain/reactive loop), never as an agent/statusline."""
    actions = dispatch([_slack_user_reply_signal()])
    assert [(a.kind, a.zone) for a in actions] == [("mechanical", "slack_user_reply")]


def test_codex_review_dispatch_standard_variant_routes_to_codex_review_agent() -> None:
    """#1254: a ``codex_review.dispatch`` signal routes to the ``codex:review`` agent.

    The agent zone matches the slash-command name so the runtime can
    invoke the same agent the user would have invoked manually.
    """
    actions = dispatch(
        [
            ScanSignal(
                kind="codex_review.dispatch",
                summary="codex review PR",
                payload={
                    "slug": "souliane/teatree",
                    "pr_id": 1254,
                    "head_sha": "abc12345",
                    "pr_url": "https://github.com/souliane/teatree/pull/1254",
                    "variant": "codex:review",
                },
            ),
        ],
    )
    agent_actions = [a for a in actions if a.kind == "agent"]
    assert len(agent_actions) == 1
    assert agent_actions[0].zone == "codex:review"
    assert agent_actions[0].payload["pr_url"] == "https://github.com/souliane/teatree/pull/1254"


def test_codex_review_dispatch_adversarial_variant_routes_to_hardened_agent() -> None:
    """#1254: ``variant == codex:adversarial-review`` routes to the hardened agent."""
    actions = dispatch(
        [
            ScanSignal(
                kind="codex_review.dispatch",
                summary="codex adversarial review PR",
                payload={
                    "slug": "souliane/teatree",
                    "pr_id": 1254,
                    "head_sha": "abc12345",
                    "pr_url": "https://github.com/souliane/teatree/pull/1254",
                    "variant": "codex:adversarial-review",
                },
            ),
        ],
    )
    agent_actions = [a for a in actions if a.kind == "agent"]
    assert len(agent_actions) == 1
    assert agent_actions[0].zone == "codex:adversarial-review"


def test_one_raising_signal_does_not_abort_the_others(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal whose routing raises is skipped + logged; the rest still route (#1649)."""
    real_dispatch_one = dispatch_module._dispatch_one

    def _flaky(signal: ScanSignal) -> list[DispatchAction]:
        if signal.kind == "boom":
            msg = "routing exploded"
            raise RuntimeError(msg)
        return real_dispatch_one(signal)

    monkeypatch.setattr(dispatch_module, "_dispatch_one", _flaky)

    with caplog.at_level(logging.ERROR, logger="teatree.loop.dispatch"):
        actions = dispatch(
            [
                ScanSignal(kind="my_pr.open", summary="PR #1 open"),
                ScanSignal(kind="boom", summary="signal that blows up"),
                ScanSignal(kind="reviewer_pr.new_sha", summary="MR x"),
            ],
        )

    zones = [(a.kind, a.zone) for a in actions]
    assert ("statusline", "in_flight") in zones
    assert ("agent", "t3:reviewer") in zones
    assert all(a.kind != "agent" or a.zone != "boom" for a in actions)
    assert any("boom" in r.message for r in caplog.records)
