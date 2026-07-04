"""Behaviour tests for IncomingEventsScanner (#669, #654)."""

import os
from unittest.mock import patch

from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.gates.merge_guard import MergeGuard
from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.core.models.incoming_event import MAX_INGEST_ATTEMPTS
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.incoming_events import IncomingEventsScanner


def _event(*, source: str, body: str, key: str, **payload_extras: object) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=source,
        actor="alice",
        channel_ref="C-eng",
        thread_ref="thread-1",
        body=body,
        payload_json=payload_extras,
        idempotency_key=key,
    )


class TestIncomingEventsScanner(TestCase):
    def test_marks_record_only_events_as_processed(self) -> None:
        event = _event(
            source=IncomingEvent.Source.CI,
            body="pipeline succeeded",
            key="ci:1",
            status="success",
        )

        signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        assert any(s.kind == "incoming_event.recorded" for s in signals)

    def test_drops_noise_and_marks_processed(self) -> None:
        event = _event(
            source=IncomingEvent.Source.SLACK,
            body="",
            key="slack:noise",
            event={"type": "team_join"},
        )

        IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None

    def test_alert_user_dispatches_via_replier(self) -> None:
        event = _event(
            source=IncomingEvent.Source.SLACK,
            body="<@bot> urgent: prod is down",
            key="slack:urgent",
            event={"type": "app_mention"},
        )

        IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        assert ReplyDispatch.objects.filter(event=event, action_name="post_dm").exists()

    def test_schedule_task_emits_action_signal(self) -> None:
        event = _event(
            source=IncomingEvent.Source.SLACK,
            body="<@bot> please implement the dashboard",
            key="slack:task1",
            event={"type": "app_mention"},
        )

        signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        kinds = {s.kind for s in signals}
        assert "incoming_event.task_needed" in kinds

    def test_task_signal_payload_carries_detail_for_review_bridge(self) -> None:
        """The task_needed payload exposes the inbound body for the bridge.

        The dispatcher needs it to spot a Slack review request and route
        it to an independent review (#219). Without ``detail`` the PR URL
        is invisible downstream.
        """
        event = _event(
            source=IncomingEvent.Source.SLACK,
            body="<@bot> can you review https://github.com/o/r/pull/5",
            key="slack:review1",
            event={"type": "app_mention"},
        )

        signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        task = next(s for s in signals if s.kind == "incoming_event.task_needed")
        assert "https://github.com/o/r/pull/5" in task.payload["detail"]

    def test_schedule_merge_emits_action_signal(self) -> None:
        event = _event(
            source=IncomingEvent.Source.GITLAB,
            body="approved",
            key="gitlab:approval",
            object_kind="merge_request",
            object_attributes={"action": "approved", "iid": 42},
        )

        signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.processed_at is not None
        kinds = {s.kind for s in signals}
        assert "incoming_event.merge_needed" in kinds

    def test_skip_already_processed_events(self) -> None:
        event = _event(
            source=IncomingEvent.Source.CI,
            body="pipeline succeeded",
            key="ci:already",
        )
        event.mark_processed()

        signals = IncomingEventsScanner().scan()

        assert signals == []

    def test_one_corrupt_event_does_not_block_the_queue(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        corrupt = _event(source=IncomingEvent.Source.CI, body="boom", key="ci:corrupt")
        healthy = _event(source=IncomingEvent.Source.CI, body="ok", key="ci:healthy")

        original = IncomingEventsScanner._handle

        def _maybe_explode(self: IncomingEventsScanner, event: IncomingEvent):
            if event.pk == corrupt.pk:
                msg = "synthetic failure"
                raise RuntimeError(msg)
            return original(self, event)

        with patch.object(IncomingEventsScanner, "_handle", _maybe_explode):
            IncomingEventsScanner().scan()

        corrupt.refresh_from_db()
        healthy.refresh_from_db()
        # The corrupt event no longer BLOCKS the queue (healthy still drains) and
        # is no longer silently dropped via mark_processed — it records the
        # failure and retries with backoff (#673).
        assert corrupt.processed_at is None
        assert corrupt.attempts == 1
        assert healthy.processed_at is not None

    def test_respects_limit(self) -> None:
        for i in range(5):
            _event(
                source=IncomingEvent.Source.CI,
                body=f"pipeline {i}",
                key=f"ci:limit-{i}",
            )

        signals = IncomingEventsScanner(limit=2).scan()

        processed_count = IncomingEvent.objects.filter(processed_at__isnull=False).count()
        assert processed_count == 2
        assert len(signals) <= 2

    def test_unmigrated_db_is_a_silent_noop(self) -> None:
        """A present-but-un-migrated DB is a silent no-op.

        With the `teatree_incoming_event` table genuinely absent (the
        pre-migration install state), `scan()` returns `[]` instead of
        raising the real engine error that `tick._run_job` would surface
        as a per-tick WARN. Drops the real table with raw DDL so the
        production query hits the real missing-relation exception (per
        AGENTS.md Test-Writing Doctrine — real DB, not a mocked
        manager); the TestCase transaction rolls the DROP back.
        """
        from django.db import connection  # noqa: PLC0415

        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE teatree_incoming_event")

        signals = IncomingEventsScanner().scan()

        assert signals == []

    # ── can_auto_merge guard (#654) ──────────────────────────────────────────

    def _merge_event(self) -> IncomingEvent:
        return _event(
            source=IncomingEvent.Source.GITLAB,
            body="approved",
            key="gitlab:guard-test",
            object_kind="merge_request",
            object_attributes={"action": "approved", "iid": 99},
        )

    def test_default_overlay_emits_merge_needed(self) -> None:
        """Default OverlayBase.can_auto_merge (permissive) → merge_needed signal."""
        self._merge_event()

        signals = IncomingEventsScanner().scan()

        kinds = {s.kind for s in signals}
        assert "incoming_event.merge_needed" in kinds
        assert "incoming_event.merge_blocked" not in kinds
        assert "incoming_event.merge_escalation" not in kinds

    def test_overlay_blocks_merge_without_escalation(self) -> None:
        """Overlay returning allowed=False, escalate=False → blocked signal, no merge."""
        self._merge_event()

        blocking_guard = MergeGuard(allowed=False, reason="not ready", escalate=False)
        mock_overlay = patch.object(
            overlay_loader_mod,
            "get_overlay",
            return_value=_StubOverlay(blocking_guard),
        )
        with mock_overlay:
            signals = IncomingEventsScanner().scan()

        kinds = {s.kind for s in signals}
        assert "incoming_event.merge_blocked" in kinds
        assert "incoming_event.merge_needed" not in kinds
        assert "incoming_event.merge_escalation" not in kinds

    def test_overlay_blocks_merge_with_escalation(self) -> None:
        """Overlay returning allowed=False, escalate=True → escalation signal, no merge."""
        self._merge_event()

        escalating_guard = MergeGuard(allowed=False, reason="human review required", escalate=True)
        mock_overlay = patch.object(
            overlay_loader_mod,
            "get_overlay",
            return_value=_StubOverlay(escalating_guard),
        )
        with mock_overlay:
            signals = IncomingEventsScanner().scan()

        kinds = {s.kind for s in signals}
        assert "incoming_event.merge_escalation" in kinds
        assert "incoming_event.merge_needed" not in kinds
        assert "incoming_event.merge_blocked" not in kinds

    def test_merge_blocked_signal_carries_refs(self) -> None:
        """Blocked signal payload carries event_id, target_ref, and thread_ref."""
        self._merge_event()

        blocking_guard = MergeGuard(allowed=False, reason="hold", escalate=False)
        with patch.object(overlay_loader_mod, "get_overlay", return_value=_StubOverlay(blocking_guard)):
            signals = IncomingEventsScanner().scan()

        blocked = next(s for s in signals if s.kind == "incoming_event.merge_blocked")
        assert "event_id" in blocked.payload
        assert "target_ref" in blocked.payload
        assert "thread_ref" in blocked.payload
        assert blocked.payload["thread_ref"] == "thread-1"

    def test_merge_escalation_signal_carries_refs(self) -> None:
        """Escalation signal payload contains event_id, target_ref, and reason."""
        self._merge_event()

        escalating_guard = MergeGuard(allowed=False, reason="policy violation", escalate=True)
        with patch.object(overlay_loader_mod, "get_overlay", return_value=_StubOverlay(escalating_guard)):
            signals = IncomingEventsScanner().scan()

        escalation = next(s for s in signals if s.kind == "incoming_event.merge_escalation")
        assert "event_id" in escalation.payload
        assert "target_ref" in escalation.payload
        assert "reason" in escalation.payload
        assert "thread_ref" in escalation.payload
        assert escalation.payload["thread_ref"] == "thread-1"


class _ParentResolverBackend:
    """MessagingBackend stub exposing one parent message keyed by ts."""

    def __init__(self, *, parent_ts: str, parent_text: str) -> None:
        self._parent_ts = parent_ts
        self._parent_text = parent_text
        self.fetched: list[str] = []

    def fetch_message(self, *, channel: str, ts: str) -> dict:
        _ = channel
        self.fetched.append(ts)
        if ts == self._parent_ts:
            return {"ts": ts, "text": self._parent_text}
        return {}


class TestThreadReplyParentContext(TestCase):
    """A threaded reply must expose its parent's text to the answerer (#2230)."""

    def _reply_event(self, *, parent_text: str = "") -> IncomingEvent:
        return IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            actor="U02ABCDEF",
            channel_ref="C024BE91L",
            thread_ref="1234567890.000100",
            parent_ts="1234567890.000100",
            parent_text=parent_text,
            body="where is the URL?",
            payload_json={"event": {"type": "message", "thread_ts": "1234567890.000100"}},
            idempotency_key="slack:Ev0THREADRPLY",
        )

    def test_parent_text_resolved_and_persisted_for_a_reply(self) -> None:
        event = self._reply_event()
        backend = _ParentResolverBackend(
            parent_ts="1234567890.000100",
            parent_text="approve posting the evidence?",
        )

        IncomingEventsScanner(messaging_resolver=lambda _overlay: backend).scan()

        event.refresh_from_db()
        assert event.parent_text == "approve posting the evidence?"
        assert "1234567890.000100" in backend.fetched

    def test_task_signal_exposes_parent_referent_to_answerer(self) -> None:
        self._reply_event()
        backend = _ParentResolverBackend(
            parent_ts="1234567890.000100",
            parent_text="approve posting the evidence?",
        )

        signals = IncomingEventsScanner(messaging_resolver=lambda _overlay: backend).scan()

        task = next(s for s in signals if s.kind == "incoming_event.task_needed")
        assert task.payload["parent_ts"] == "1234567890.000100"
        assert task.payload["parent_text"] == "approve posting the evidence?"

    def test_already_persisted_parent_text_skips_the_backend_fetch(self) -> None:
        self._reply_event(parent_text="approve posting the evidence?")
        backend = _ParentResolverBackend(parent_ts="1234567890.000100", parent_text="STALE")

        signals = IncomingEventsScanner(messaging_resolver=lambda _overlay: backend).scan()

        task = next(s for s in signals if s.kind == "incoming_event.task_needed")
        assert task.payload["parent_text"] == "approve posting the evidence?"
        assert backend.fetched == []

    def test_no_backend_leaves_parent_text_blank_but_still_routes(self) -> None:
        event = self._reply_event()

        signals = IncomingEventsScanner(messaging_resolver=lambda _overlay: None).scan()

        event.refresh_from_db()
        assert event.parent_text == ""
        task = next(s for s in signals if s.kind == "incoming_event.task_needed")
        assert task.payload["parent_ts"] == "1234567890.000100"
        assert task.payload["parent_text"] == ""

    def test_backend_raise_is_swallowed_and_routing_continues(self) -> None:
        event = self._reply_event()

        def _raising(_overlay: str) -> object:
            class _Raises:
                def fetch_message(self, *, channel: str, ts: str) -> dict:
                    _ = (channel, ts)
                    msg = "history 503"
                    raise RuntimeError(msg)

            return _Raises()

        signals = IncomingEventsScanner(messaging_resolver=_raising).scan()

        event.refresh_from_db()
        assert event.parent_text == ""
        assert any(s.kind == "incoming_event.task_needed" for s in signals)

    def test_parent_message_without_text_leaves_field_blank(self) -> None:
        event = self._reply_event()
        backend = _ParentResolverBackend(parent_ts="other.ts", parent_text="unused")

        IncomingEventsScanner(messaging_resolver=lambda _overlay: backend).scan()

        event.refresh_from_db()
        assert event.parent_text == ""

    def test_root_message_is_not_resolved_against_the_backend(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            actor="U02ABCDEF",
            channel_ref="C024BE91L",
            thread_ref="1234567890.000100",
            body="can you implement the dashboard?",
            payload_json={"event": {"type": "message"}},
            idempotency_key="slack:Ev0ROOTTASK",
        )
        backend = _ParentResolverBackend(parent_ts="x", parent_text="x")

        IncomingEventsScanner(messaging_resolver=lambda _overlay: backend).scan()

        assert backend.fetched == []


class _StubOverlay:
    """Minimal overlay stub that returns a fixed MergeGuard from can_auto_merge."""

    def __init__(self, guard: MergeGuard) -> None:
        self._guard = guard

    def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
        return self._guard


class _MergeGuardOverlay(OverlayBase):
    """Concrete overlay owning a repo and returning a fixed merge guard."""

    def __init__(self, *, repos: list[str], guard: MergeGuard) -> None:
        self._repos = repos
        self._guard = guard

    def get_repos(self) -> list[str]:
        return self._repos

    def get_workspace_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree: object) -> list:
        _ = worktree
        return []

    def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
        _ = (target_ref, thread_ref)
        return self._guard


class TestEventForgeUrl:
    """``_event_forge_url`` extracts the forge URL/slug from a webhook event (TODO-282)."""

    def _event(self, **payload: object) -> IncomingEvent:
        return IncomingEvent(channel_ref="acme/repo", payload_json=dict(payload))

    def test_prefers_gitlab_object_attributes_url(self) -> None:
        from teatree.loop.scanners.incoming_events import _event_forge_url  # noqa: PLC0415

        url = "https://gitlab.com/acme/backend/-/merge_requests/9"
        event = self._event(object_attributes={"url": url})
        assert _event_forge_url(event) == url

    def test_prefers_github_pull_request_html_url(self) -> None:
        from teatree.loop.scanners.incoming_events import _event_forge_url  # noqa: PLC0415

        url = "https://github.com/acme/backend/pull/9"
        event = self._event(pull_request={"html_url": url})
        assert _event_forge_url(event) == url

    def test_falls_back_to_channel_ref_slug(self) -> None:
        from teatree.loop.scanners.incoming_events import _event_forge_url  # noqa: PLC0415

        event = self._event()
        assert _event_forge_url(event) == "acme/repo"

    def test_empty_when_no_url_and_no_channel_ref(self) -> None:
        from teatree.loop.scanners.incoming_events import _event_forge_url  # noqa: PLC0415

        event = IncomingEvent(channel_ref="", payload_json={})
        assert _event_forge_url(event) == ""

    def test_non_string_url_falls_through(self) -> None:
        from teatree.loop.scanners.incoming_events import _event_forge_url  # noqa: PLC0415

        event = self._event(object_attributes={"url": 123}, pull_request={"html_url": None})
        assert _event_forge_url(event) == "acme/repo"


class TestScheduleMergeMultiOverlay(TestCase):
    """Real ``get_overlay()`` ambiguity path — two overlays registered (TODO-282).

    ``_handle_schedule_merge`` applies a *per-overlay* merge policy
    (``can_auto_merge``) but resolved it with a bare ``get_overlay()``. With
    two overlays registered and no ``T3_OVERLAY_NAME`` that raises ``Multiple
    overlays found`` — swallowed by ``scan()``'s per-event ``except Exception``,
    which marks the event processed and drops the approved merge. The fix
    resolves the overlay from the event's forge URL (carried in the GitLab
    webhook ``object_attributes.url``), so the URL-owning overlay's policy runs.

    Only overlay discovery is patched (the entry-point external); the URL→overlay
    resolution itself is real. The owning overlay returns an ESCALATE guard so
    the emitted signal proves *that overlay's* policy ran, not a permissive default.
    """

    def test_resolves_url_owning_overlay_with_two_registered(self) -> None:
        url = "https://gitlab.com/acme/backend/-/merge_requests/88"
        _event(
            source=IncomingEvent.Source.GITLAB,
            body="approved",
            key="gitlab:multi-overlay",
            object_kind="merge_request",
            object_attributes={"action": "approved", "iid": 88, "url": url},
        )
        owner = _MergeGuardOverlay(
            repos=["acme/backend"],
            guard=MergeGuard(allowed=False, reason="freeze window", escalate=True),
        )
        other = _MergeGuardOverlay(repos=["other/repo"], guard=MergeGuard(allowed=True))
        overlays = {"acme": owner, "other": other}
        env_without_pin = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
        with (
            patch.dict(os.environ, env_without_pin, clear=True),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays),
        ):
            signals = IncomingEventsScanner().scan()

        escalations = [s for s in signals if s.kind == "incoming_event.merge_escalation"]
        assert len(escalations) == 1, (
            "with two overlays registered, the approved merge must still resolve the URL-owning "
            "overlay's policy — a bare get_overlay() raises Multiple-overlays and drops the merge"
        )
        assert escalations[0].payload["reason"] == "freeze window"


class TestIncomingEventsScannerReliability(TestCase):
    """A failed drain retries with backoff and eventually dead-letters (#673)."""

    def test_failed_drain_retries_instead_of_silently_dropping(self) -> None:
        event = _event(source=IncomingEvent.Source.SLACK, body="x", key="slack:boom", event={"type": "app_mention"})

        with patch.object(IncomingEventsScanner, "_handle", side_effect=ValueError("boom")):
            signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        # NOT dropped: previously mark_processed() hid the poison; now it retries.
        assert event.processed_at is None
        assert event.attempts == 1
        assert event.next_retry_at is not None
        assert event.dead_lettered_at is None
        assert not any(s.kind == "incoming_event.dead_letter" for s in signals)

    def test_exhausted_retries_dead_letter_and_emit_surface_signal(self) -> None:
        event = _event(source=IncomingEvent.Source.SLACK, body="x", key="slack:poison", event={"type": "app_mention"})
        event.attempts = MAX_INGEST_ATTEMPTS - 1
        event.save(update_fields=["attempts"])

        with patch.object(IncomingEventsScanner, "_handle", side_effect=ValueError("boom")):
            signals = IncomingEventsScanner().scan()

        event.refresh_from_db()
        assert event.is_dead_lettered is True
        dead = [s for s in signals if s.kind == "incoming_event.dead_letter"]
        assert len(dead) == 1
        assert dead[0].payload["event_id"] == event.pk

    def test_dead_lettered_event_is_no_longer_drained(self) -> None:
        event = _event(source=IncomingEvent.Source.SLACK, body="x", key="slack:done", event={"type": "app_mention"})
        for _ in range(MAX_INGEST_ATTEMPTS):
            event.record_failure("boom", now=None)

        with patch.object(IncomingEventsScanner, "_handle", side_effect=AssertionError("must not re-drain")):
            IncomingEventsScanner().scan()  # a dead-lettered event must never reach _handle again
