"""The review-claim chokepoint honours the DB LoopState tier (#1913).

``review_loop_enabled`` is the gate the review-claim chokepoint reads before
queueing a reviewer dispatch (``filter_review_intent_signals``). #1913 makes the
DB ``LoopState`` the canonical control tier above the env/toml config: a
``PAUSED`` / ``DISABLED`` row on the ``review`` loop durably stops review claims
across a restart, while an empty table / ENABLED row falls through to the
env/toml behaviour (no regression).

``loop_held_in_db`` is the single ORM read both this chokepoint and the tick gate
consult, so the "is the review loop stopped?" answer cannot drift between them.
"""

import pytest

from teatree.core.models import LoopState
from teatree.loop.loop_state_db import loop_held_in_db
from teatree.loop.review_claim import review_loop_enabled

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _env_toml_says_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the env/toml tier to ENABLED so these tests exercise ONLY the #1913 DB
    # tier, independent of whatever the host's real ``[loops.review]`` config
    # says (the user's config may disable review). ``review_loop_enabled``
    # imports ``loop_enabled_by_name`` lazily inside the function, so patch it on
    # its source module.
    monkeypatch.setattr("teatree.loop_enabled.loop_enabled_by_name", lambda *_a, **_k: True)


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
        # No row → no regression: the env/toml default is enabled.
        assert review_loop_enabled() is True

    def test_db_resume_re_enables_review_claims(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        assert review_loop_enabled() is True
