"""``dream_fallen_behind`` — the reportable-stall predicate behind the health chip (#4726).

The shipped ``dream`` row is ``default_enabled=false`` and the ``low-token`` / ``off``
presets mask it, so an ADMITTED loop is the precondition for every emitting case here, not
the ambient state of a fresh DB.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from teatree.core.factory.dream_staleness import dream_fallen_behind
from teatree.core.models import Loop, LoopState, Prompt
from teatree.core.models.dream_run_marker import CRITICAL_STALE_MULTIPLE, STALE_THRESHOLD_HOURS, DreamRunMarker

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _dream_loop(*, enabled: bool) -> Loop:
    """The dream ``Loop`` row in a known enabled state — seeded or not, migrations or not."""
    prompt, _ = Prompt.objects.get_or_create(name="dream", defaults={"body": "consolidate"})
    return Loop.objects.update_or_create(
        name=DreamRunMarker.NAME,
        defaults={"prompt": prompt, "script": "", "delay_seconds": 86400, "enabled": enabled},
    )[0]


def _succeeded_hours_ago(hours: float) -> None:
    DreamRunMarker.objects.mark_succeeded(timezone.now() - timedelta(hours=hours))


class TestAnAdmittedLoopReportsItsStall:
    @pytest.fixture(autouse=True)
    def _admitted(self) -> None:
        _dream_loop(enabled=True)

    def test_a_recent_success_is_not_behind(self) -> None:
        _succeeded_hours_ago(1)

        assert dream_fallen_behind(timezone.now()) is None

    def test_past_the_window_is_behind_but_not_critical(self) -> None:
        _succeeded_hours_ago(STALE_THRESHOLD_HOURS + 1)

        behind = dream_fallen_behind(timezone.now())

        assert behind is not None
        assert behind.critical is False

    def test_past_the_critical_multiple_is_critical(self) -> None:
        _succeeded_hours_ago(STALE_THRESHOLD_HOURS * CRITICAL_STALE_MULTIPLE + 1)

        behind = dream_fallen_behind(timezone.now())

        assert behind is not None
        assert behind.critical is True

    def test_it_carries_the_marker_timestamp_the_summary_quotes(self) -> None:
        _succeeded_hours_ago(STALE_THRESHOLD_HOURS + 1)
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)

        behind = dream_fallen_behind(timezone.now())

        assert behind is not None
        assert behind.last_succeeded_at == marker.last_succeeded_at


class TestBootstrapHasNoBaselineToRegressFrom:
    """Mirrors ``is_critically_stale``'s exclusion, not ``is_stale``'s advisory reading."""

    @pytest.fixture(autouse=True)
    def _admitted(self) -> None:
        _dream_loop(enabled=True)

    def test_no_marker_row_is_not_behind(self) -> None:
        assert not DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).exists()

        assert dream_fallen_behind(timezone.now()) is None

    def test_a_marker_that_only_ever_attempted_is_not_behind(self) -> None:
        DreamRunMarker.objects.update_or_create(
            name=DreamRunMarker.NAME,
            defaults={"last_succeeded_at": None, "last_attempted_at": timezone.now()},
        )

        assert dream_fallen_behind(timezone.now()) is None


class TestADeliberateOffIsANoteNotAFault:
    """Every arm the enable verdict refuses on — the one #4196 says must not fork."""

    @pytest.fixture(autouse=True)
    def _critically_stale(self) -> None:
        _succeeded_hours_ago(STALE_THRESHOLD_HOURS * CRITICAL_STALE_MULTIPLE + 1)

    def test_the_shipped_disabled_row_is_not_behind(self) -> None:
        _dream_loop(enabled=False)

        assert dream_fallen_behind(timezone.now()) is None

    def test_a_loopstate_pause_is_not_behind(self) -> None:
        _dream_loop(enabled=True)
        LoopState.objects.pause(DreamRunMarker.NAME)

        assert dream_fallen_behind(timezone.now()) is None

    def test_a_loopstate_disable_is_not_behind(self) -> None:
        _dream_loop(enabled=True)
        LoopState.objects.disable(DreamRunMarker.NAME)

        assert dream_fallen_behind(timezone.now()) is None

    def test_a_force_off_override_is_not_behind(self) -> None:
        _dream_loop(enabled=True)
        LoopState.objects.override(DreamRunMarker.NAME, on=False)

        assert dream_fallen_behind(timezone.now()) is None

    def test_a_missing_loop_row_is_not_behind(self) -> None:
        Loop.objects.filter(name=DreamRunMarker.NAME).delete()

        assert dream_fallen_behind(timezone.now()) is None

    def test_an_admitted_loop_is_still_behind(self) -> None:
        _dream_loop(enabled=True)

        assert dream_fallen_behind(timezone.now()) is not None
