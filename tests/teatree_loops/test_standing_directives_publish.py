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
from teatree.core.models import Mode, Prompt
from teatree.loop.standing_directives import DISPATCH_LOOP, override_prompt_name, resolve_standing_directives
from teatree.loops import timer_reconciler
from teatree.loops.standing_directives_publish import (
    PUBLISH_POLL_SECONDS,
    ensure_standing_directives_publish_chain,
    publish_standing_directives,
)

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
