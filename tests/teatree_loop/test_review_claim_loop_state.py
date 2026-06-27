"""The review-claim chokepoint honours the DB LoopState tier (#1913).

``review_loop_enabled`` is the gate the review-claim chokepoint reads before
queueing a reviewer dispatch (``filter_review_intent_signals``). The DB
``LoopState`` tier is the single (and only) control authority: a ``PAUSED`` /
``DISABLED`` row on the ``review`` loop durably stops review claims across a
restart, while an empty table / ENABLED row leaves them running (no regression).
There is no env kill-switch — loop control is ``/loops`` + the DB only.

``loop_held_in_db`` is the single ORM read both this chokepoint and the tick gate
consult, so the "is the review loop stopped?" answer cannot drift between them.
"""

import pytest

from teatree.core.models import LoopState
from teatree.loop.loop_state_db import loop_held_in_db
from teatree.loop.review_claim import review_loop_enabled

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestLoopHeldInDb:
    def test_paused_loop_is_held(self) -> None:
        LoopState.objects.pause("review")
        assert loop_held_in_db("review") is True

    def test_disabled_loop_is_held(self) -> None:
        LoopState.objects.disable("review")
        assert loop_held_in_db("review") is True

    def test_absent_loop_is_not_held(self) -> None:
        assert loop_held_in_db("review") is False

    def test_resumed_loop_is_not_held(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        assert loop_held_in_db("review") is False


class TestReviewLoopEnabledHonoursDb:
    def test_db_pause_stops_review_claims(self) -> None:
        LoopState.objects.pause("review")
        assert review_loop_enabled() is False

    def test_db_disable_stops_review_claims(self) -> None:
        LoopState.objects.disable("review")
        assert review_loop_enabled() is False

    def test_empty_table_leaves_review_enabled(self) -> None:
        # No row → no regression: the default is enabled (DB-only control).
        assert review_loop_enabled() is True

    def test_db_resume_re_enables_review_claims(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        assert review_loop_enabled() is True
