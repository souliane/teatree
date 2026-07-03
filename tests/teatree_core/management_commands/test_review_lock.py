"""``t3 <overlay> review lock-acquire`` / ``review lock-status`` — the manual Agent() dispatch lock seam (#1405)."""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import MRReviewLock

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_URL = "https://github.com/souliane/teatree/pull/1405"
_SLUG = "souliane/teatree"
_PR = 1405


def _acquire(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {"holder": "t3:reviewer-agent-a"}
    kwargs.update(overrides)
    return cast("dict[str, object]", call_command("review", "lock-acquire", _URL, **kwargs))


def _status() -> dict[str, object]:
    return cast("dict[str, object]", call_command("review", "lock-status", _URL))


class TestLockAcquireCommand(TestCase):
    def test_first_acquire_succeeds(self) -> None:
        result = _acquire()

        assert result["acquired"] is True
        assert result["slug"] == _SLUG
        assert result["pr_id"] == _PR
        assert result["state"] == MRReviewLock.State.REVIEW_DISPATCHED

    def test_second_acquire_while_held_is_a_no_op_naming_the_holder(self) -> None:
        _acquire(holder="t3:reviewer-agent-a")

        result = _acquire(holder="t3:reviewer-agent-b")

        assert result["acquired"] is False
        assert result["holder"] == "t3:reviewer-agent-a"
        assert result["state"] == MRReviewLock.State.REVIEW_DISPATCHED
        assert MRReviewLock.objects.count() == 1

    def test_missing_holder_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            call_command("review", "lock-acquire", _URL)

    def test_unparseable_url_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            call_command("review", "lock-acquire", "not-a-url", holder="t3:reviewer-agent-a")

    def test_acquire_after_resolve_succeeds_for_a_new_dispatcher(self) -> None:
        _acquire(holder="t3:reviewer-agent-a")
        MRReviewLock.resolve(slug=_SLUG, pr_id=_PR)

        result = _acquire(holder="t3:reviewer-agent-b")

        assert result["acquired"] is True
        assert result["holder"] == "t3:reviewer-agent-b"


class TestLockStatusCommand(TestCase):
    def test_no_lock_reports_idle(self) -> None:
        result = _status()

        assert result["locked"] is False
        assert result["state"] == "idle"

    def test_held_lock_reports_locked_with_holder(self) -> None:
        _acquire(holder="t3:reviewer-agent-a")

        result = _status()

        assert result["locked"] is True
        assert result["state"] == MRReviewLock.State.REVIEW_DISPATCHED
        assert result["holder"] == "t3:reviewer-agent-a"

    def test_resolved_lock_reports_unlocked(self) -> None:
        _acquire(holder="t3:reviewer-agent-a")
        MRReviewLock.resolve(slug=_SLUG, pr_id=_PR)

        result = _status()

        assert result["locked"] is False
        assert result["state"] == MRReviewLock.State.RESOLVED
