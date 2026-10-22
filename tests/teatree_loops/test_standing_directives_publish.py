"""teatree.loops.standing_directives_publish — keeps the hooks' standing-directive cache current.

The hooks read only the published file, so an owner's edit in the admin and a mode
change reach them through this chain. Integration-first against the real DB and the
``django_tasks_db`` backend.
"""

import datetime as dt
from unittest import mock

import django.test
import pytest
from django.tasks import TaskResultStatus
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree import standing_directives_cache
from teatree.core.mode_resolution import ResolvedMode
from teatree.core.modelkit.relay import RELAY_LISTING_CHARS
from teatree.core.models import DmContext, Mode, PendingChatInjection, Prompt, Task
from teatree.loop.standing_directives import DISPATCH_LOOP, override_prompt_name, resolve_standing_directives
from teatree.loops import timer_reconciler
from teatree.loops.standing_directives_publish import (
    PUBLISH_POLL_SECONDS,
    ensure_standing_directives_publish_chain,
    publish_standing_directives,
)
from teatree.utils.url_slug import slack_conversation_anchor
from tests._factory_slack import OWNER_DM, OWNER_ID, SLACK_OVERLAY, FakeOwnerDm, live_factory_slack
from tests.factories import TaskFactory, TicketFactory

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}


def _pending() -> list[DBTaskResult]:
    return list(
        DBTaskResult.objects.filter(task_path=publish_standing_directives.module_path, status=TaskResultStatus.READY)
    )


def _published_slots() -> list[str] | None:
    published = standing_directives_cache.read()
    return None if published is None else [directive["slot_id"] for directive in published]


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestThePublishChain(django.test.TestCase):
    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def _fire(self) -> dict[str, str]:
        DBTaskResult.objects.filter(task_path=publish_standing_directives.module_path).delete()
        return publish_standing_directives.func()

    def test_a_fire_publishes_and_re_arms(self) -> None:
        before = timezone.now()

        assert self._fire() == {"action": "published"}

        assert standing_directives_cache.read() == [d.as_dict() for d in resolve_standing_directives()]
        [successor] = _pending()
        assert successor.run_after >= before + dt.timedelta(seconds=PUBLISH_POLL_SECONDS)

    def test_an_unchanged_resolution_is_reported_and_not_rewritten(self) -> None:
        self._fire()

        assert self._fire() == {"action": "unchanged"}

    def test_an_owner_edit_reaches_the_hooks_on_the_next_fire(self) -> None:
        self._fire()
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="")

        assert self._fire() == {"action": "published"}
        assert _published_slots() == ["standing-golden-rule", "standing-todo-consolidate"]

    def test_a_mode_change_reaches_the_hooks_on_the_next_fire(self) -> None:
        self._fire()
        paused = ResolvedMode(
            mode=Mode(name="off", entries={DISPATCH_LOOP: False}), source="override", until=None, reason="test"
        )

        with mock.patch("teatree.core.mode_resolution.resolve_active_mode", return_value=paused):
            self._fire()

        assert _published_slots() == ["standing-golden-rule"]

    def test_a_second_pending_fire_dedups_and_publishes_nothing(self) -> None:
        publish_standing_directives.using(run_after=timezone.now()).enqueue()

        assert publish_standing_directives.func() == {"action": "deduped"}
        assert standing_directives_cache.read() is None

    def test_a_failed_publication_still_leaves_the_successor_armed(self) -> None:
        with (
            mock.patch.object(standing_directives_cache, "publish", side_effect=OSError("read-only data dir")),
            pytest.raises(OSError, match="read-only data dir"),
        ):
            publish_standing_directives.func()

        assert len(_pending()) == 1


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestSeeding(django.test.TestCase):
    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def test_seeding_is_idempotent(self) -> None:
        ensure_standing_directives_publish_chain()
        ensure_standing_directives_publish_chain()

        assert len(_pending()) == 1

    def test_the_first_publication_is_not_a_poll_away(self) -> None:
        ensure_standing_directives_publish_chain()

        [head] = _pending()
        assert head.run_after <= timezone.now()

    def test_a_worker_start_seeds_it(self) -> None:
        timer_reconciler.ensure_maintenance_chains()
        timer_reconciler.ensure_maintenance_chains()

        assert len(_pending()) == 1


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestTheFactorySlackVerdictRidesTheSameChain(django.test.TestCase):
    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def _fire(self) -> None:
        DBTaskResult.objects.filter(task_path=publish_standing_directives.module_path).delete()
        publish_standing_directives.func()

    def test_a_fire_publishes_a_live_verdict_and_each_sessions_open_relays(self) -> None:
        silent = PendingChatInjection.record(
            channel=OWNER_DM,
            slack_ts="1.0",
            text="  is it\nmerged?  ",
            overlay=SLACK_OVERLAY,
            context=DmContext(user_id=OWNER_ID, relayed_by_session="s1"),
        )
        assert silent is not None
        PendingChatInjection.objects.filter(pk=silent.pk).update(received_at=timezone.now() - dt.timedelta(minutes=10))

        with live_factory_slack(FakeOwnerDm()):
            self._fire()

        published = standing_directives_cache.read_factory_slack()
        assert published is not None
        assert (published["live"], published["overlay"], published["reason"]) == (True, SLACK_OVERLAY, "")
        assert published["open_relays"] == {
            "s1": [
                {
                    "id": silent.pk,
                    "slack_ts": "1.0",
                    "channel": OWNER_DM,
                    "text": "is it merged?",
                    "state": "unanswered",
                }
            ]
        }

    def test_a_failed_delegation_is_published_as_delegated_and_cut_to_the_listing_length(self) -> None:
        failed = PendingChatInjection.record(
            channel=OWNER_DM,
            slack_ts="2.0",
            text="x" * (RELAY_LISTING_CHARS + 30),
            overlay=SLACK_OVERLAY,
            context=DmContext(user_id=OWNER_ID, relayed_by_session="s1"),
        )
        assert failed is not None
        PendingChatInjection.objects.filter(pk=failed.pk).update(
            answer_kind=PendingChatInjection.AnswerKind.DELEGATED,
            received_at=timezone.now() - dt.timedelta(minutes=10),
            loop_response_confirmed_at=timezone.now(),
        )
        ticket = TicketFactory(issue_url=slack_conversation_anchor(channel=OWNER_DM, slack_ts="2.0"))
        TaskFactory(ticket=ticket, phase="answering", status=Task.Status.FAILED)

        with live_factory_slack(FakeOwnerDm()):
            self._fire()

        published = standing_directives_cache.read_factory_slack()
        assert published is not None
        [entry] = published["open_relays"]["s1"]
        assert (entry["state"], entry["text"]) == ("delegated", "x" * RELAY_LISTING_CHARS)

    def test_two_credentialed_overlays_publish_a_verdict_for_each_and_the_ambiguous_default(self) -> None:
        with live_factory_slack(FakeOwnerDm(), also_credentialed=("t3-quiet",)):
            self._fire()

        published = standing_directives_cache.read_factory_slack()
        assert published is not None
        assert not published["live"]
        assert published["reason"].startswith("ambiguous_overlay")
        assert published["by_overlay"] == {SLACK_OVERLAY: True, "t3-quiet": False}
        assert standing_directives_cache.slack_is_live(published, SLACK_OVERLAY)
        assert not standing_directives_cache.slack_is_live(published, "t3-quiet")
        assert not standing_directives_cache.slack_is_live(published, "")

    def test_a_box_without_slack_publishes_not_live_with_the_reason(self) -> None:
        with mock.patch("teatree.core.backend_factory.OverlayConfigResolver.all_names", return_value=[]):
            self._fire()

        published = standing_directives_cache.read_factory_slack()
        assert published is not None
        assert not published["live"]
        assert published["reason"].startswith("no_messaging_backend")

    def test_each_fire_advances_checked_at_even_when_nothing_else_changed(self) -> None:
        with (
            mock.patch("teatree.core.backend_factory.OverlayConfigResolver.all_names", return_value=[]),
            mock.patch("teatree.loops.standing_directives_publish.time") as clock,
        ):
            clock.time.side_effect = [1000.0, 5000.0]
            self._fire()
            first = standing_directives_cache.read_factory_slack(now=1000.0)
            self._fire()

        second = standing_directives_cache.read_factory_slack(now=5000.0)
        assert first is not None
        assert second is not None
        assert second["checked_at"] > first["checked_at"]

    def test_a_failure_publishing_the_verdict_leaves_the_directives_and_the_successor(self) -> None:
        with mock.patch.object(standing_directives_cache, "write_factory_slack", side_effect=OSError("read-only")):
            self._fire()

        assert standing_directives_cache.read() is not None
        assert len(_pending()) == 1
