"""RED tests for four confirmed bugs — written before fixes to verify reproduction.

M1 — _sweep_white_check_mark crosses overlay boundaries (missing overlay= filter)
M4 — _fetch_review_state raises TypeError when get_draft_notes_count returns None
M6 — _decode_pr collapses missing/None number to pr_id=0, poisoning the marker table
L1 — bounded post-failure backoff for the architectural-review cadence: a FAILED
review re-fires after retry_backoff_hours (not the full week, not hourly)
"""

import json
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli.review import review_app
from teatree.core.models import BroadcastObservation, ScannedBroadcast
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.architectural_review import ARCHITECTURAL_REVIEW_PHASE, ArchitecturalReviewScanner
from teatree.loop.scanners.codex_review import _decode_pr
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CHANNEL_A = "CA00000001"
CHANNEL_B = "CB00000002"
TS_X = "1779300000.000001"
TS_Y = "1779300000.000002"
MR_URL = "https://gitlab.example.com/team/repo/-/merge_requests/9001"

OVERLAY_A = "acme"
OVERLAY_B = "acme-backend"


@dataclass
class FakeMessaging:
    user_id: str = "UFAKEUSER1"
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        return {}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        return {}

    def open_dm(self, user_id: str) -> str:
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def resolve_user_id(self, handle: str) -> str:
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


def _fetcher(messages_by_channel: dict[str, list[RawAPIDict]]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        return list(messages_by_channel.get(channel, []))

    return fetch


def _classifier(states: dict[str, MrState]):
    def classify(urls):
        return [states[url] for url in urls]

    return classify


def _message(text: str, ts: str) -> RawAPIDict:
    return {"text": text, "ts": ts, "user": "USRG", "type": "message"}


# ---------------------------------------------------------------------------
# M1 — _sweep_white_check_mark crosses overlay boundaries
# ---------------------------------------------------------------------------


class TestM1SweepWhiteCheckMarkOverlayIsolation(TestCase):
    """Overlay A's all-merged sweep must NOT pick up Overlay B's ScannedBroadcast row."""

    def _seed_all_merged_broadcast(self, *, channel: str, ts: str, overlay: str) -> ScannedBroadcast:
        obs = BroadcastObservation(
            channel=channel,
            slack_ts=ts,
            mr_urls=[MR_URL],
            classification=ScannedBroadcast.Classification.ALL_MERGED.value,
            overlay=overlay,
        )
        row = ScannedBroadcast.record(obs)
        assert row is not None
        return row

    def test_sweep_does_not_react_on_foreign_overlay_broadcast(self) -> None:
        """Overlay B's all-merged broadcast must not receive a reaction from Overlay A's scanner."""
        # Seed Overlay B's broadcast row for the same MR URL.
        self._seed_all_merged_broadcast(channel=CHANNEL_B, ts=TS_Y, overlay=OVERLAY_B)

        # Overlay A scanner processes a new all-merged broadcast in CHANNEL_A.
        backend = FakeMessaging()
        history = {CHANNEL_A: [_message(f"review {MR_URL}", TS_X)]}
        states = {MR_URL: MrState(url=MR_URL, merged=True, approved=True)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL_A],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            overlay=OVERLAY_A,
        )

        scanner.scan()

        # Should only react on Overlay A's own broadcast (CHANNEL_A / TS_X).
        reacted_on = [(ch, ts) for (ch, ts, _emoji) in backend.react_calls]
        assert (CHANNEL_B, TS_Y) not in reacted_on, (
            "Overlay A's sweep reacted on Overlay B's broadcast — missing overlay= filter"
        )


# ---------------------------------------------------------------------------
# M4 — draft_notes TypeError when get_draft_notes_count returns None
# ---------------------------------------------------------------------------

type JSONObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class _ProjectInfo:
    project_id: int
    full_path: str


class TestM4DraftNotesNoneReturnsStructuredResult:
    """get_draft_notes_count returning None must not raise TypeError."""

    def test_none_draft_count_returns_structured_json_not_traceback(self) -> None:
        runner = CliRunner()

        class _NullDraftAPI:
            def get_json(self, endpoint: str) -> object:
                if endpoint.endswith("/changes"):
                    return {
                        "changes": [
                            {"new_path": "src/foo.py", "diff": "@@ -1 +1 @@\n+new\n-old"},
                        ]
                    }
                return None

            def resolve_project(self, repo: str) -> _ProjectInfo:
                return _ProjectInfo(project_id=42, full_path=repo)

            def get_mr_discussions(self, project_id: int, mr_iid: int) -> list[JSONObject]:
                return []

            def get_draft_notes_count(self, project_id: int, mr_iid: int) -> None:
                return None  # Non-Premium GitLab or swallowed timeout

            def get_mr_approvals(self, project_id: int, mr_iid: int) -> JSONObject:
                return {"count": 0, "required": 1, "approved_by": []}

        url = "https://gitlab.com/org/proj/-/merge_requests/77"
        with (
            patch("teatree.backends.gitlab.api.GitLabAPI", return_value=_NullDraftAPI()),
            patch("teatree.cli.review.service.ReviewService.get_gitlab_token", return_value="t"),
        ):
            result = runner.invoke(review_app, ["run", url])

        assert result.exit_code == 0, (
            f"Expected exit 0 but got {result.exit_code}; output={result.output!r} exc={result.exception!r}"
        )
        # STDOUT only: `review run` promises JSON there, while unrelated advisories (the
        # once-per-process host-projection notice) correctly go to stderr. Parsing the
        # merged stream made this order-dependent — whichever test first triggered that
        # advisory read it as trailing JSON and failed.
        payload = json.loads(result.stdout.strip())
        assert payload["existing_review"]["draft_notes"] == 0


# ---------------------------------------------------------------------------
# M6 — _decode_pr collapses None/missing number to pr_id=0
# ---------------------------------------------------------------------------


class TestM6DecodePrMissingNumber:
    """_decode_pr with a None or absent number must return None (skip), not pr_id=0."""

    def test_none_number_returns_none(self) -> None:
        result = _decode_pr(slug="souliane/teatree", raw={"number": None})
        assert result is None, f"Expected None but got {result!r}"

    def test_missing_number_returns_none(self) -> None:
        result = _decode_pr(slug="souliane/teatree", raw={})
        assert result is None, f"Expected None but got {result!r}"

    def test_none_number_does_not_create_pr_id_zero_marker(self) -> None:
        """A malformed PR payload must not claim a pr_id=0 marker row."""
        result = _decode_pr(slug="souliane/teatree", raw={"number": None})
        assert result is None
        assert not CodexReviewMarker.objects.filter(slug="souliane/teatree", pr_id=0).exists()


# ---------------------------------------------------------------------------
# L1 — _last_review_completed_at counts FAILED tasks
# ---------------------------------------------------------------------------


def _scanner_l1(*, cadence_hours: int = 168, retry_backoff_hours: int = 12) -> ArchitecturalReviewScanner:
    return ArchitecturalReviewScanner(
        overlay_name=OVERLAY_A,
        cadence_hours=cadence_hours,
        retry_backoff_hours=retry_backoff_hours,
        after_merge_count=999,
    )


def _seed_review_task(*, status: Task.Status, hours_ago: float, overlay: str = OVERLAY_A) -> Task:
    """Seed a terminal architectural-review task whose Session started ``hours_ago`` ago."""
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=f"architectural-review://{overlay}",
        defaults={"overlay": overlay, "role": "author"},
    )
    session = Session.objects.create(overlay=overlay, ticket=ticket, agent_id="arch")
    Session.objects.filter(pk=session.pk).update(started_at=timezone.now() - timedelta(hours=hours_ago))
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase=ARCHITECTURAL_REVIEW_PHASE,
        status=status,
    )


class TestL1BoundedPostFailureBackoff(TestCase):
    """The architectural-review cadence re-fires a FAILED review after a bounded backoff.

    The expensive full-codebase review mini-loop ticks hourly, but the internal
    gate limits firing to two clocks: the last COMPLETED review drives the full
    ``cadence_hours`` (168h) success gate, and the last terminal attempt of any
    status drives a shorter ``retry_backoff_hours`` (12h) backoff gate. A review
    fires only when BOTH have elapsed. So a transient failure retries in 12h (no
    week-long blind spot), a persistently failing review backs off to every 12h
    instead of storming hourly, and a completed review still suppresses for the
    full week.
    """

    def test_recent_failure_within_backoff_is_suppressed(self) -> None:
        """A FAILED review 30 min old must NOT re-dispatch — the backoff has not elapsed.

        Anti-vacuous: if the backoff were ignored (hourly storm), the completed
        clock would be None → bootstrap → a fresh expensive review every tick.
        """
        _seed_review_task(status=Task.Status.FAILED, hours_ago=0.5)

        assert _scanner_l1().scan() == []

    def test_failure_past_backoff_redispatches(self) -> None:
        """A FAILED review 13 h old (no completed review since) MUST re-dispatch.

        Anti-vacuous: if a failure suppressed for the full 168h week (the old
        completed-only clock with no backoff bound, or treating a failure like a
        completed review), this would return [] and leave a 7-day blind spot.
        """
        _seed_review_task(status=Task.Status.FAILED, hours_ago=13)

        signals = _scanner_l1().scan()

        assert len(signals) == 1

    def test_completed_review_suppresses_for_full_week(self) -> None:
        """A COMPLETED review 13 h old (past the 12h backoff) still suppresses.

        Anti-vacuous: if the 12h backoff were the only gate, a completed review
        older than 12h would wrongly re-fire long before its 168h cadence.
        """
        _seed_review_task(status=Task.Status.COMPLETED, hours_ago=13)

        assert _scanner_l1().scan() == []

    def test_completed_review_past_cadence_redispatches(self) -> None:
        """A COMPLETED review older than the 168h cadence re-fires (success clock intact)."""
        _seed_review_task(status=Task.Status.COMPLETED, hours_ago=169)

        signals = _scanner_l1().scan()

        assert len(signals) == 1
