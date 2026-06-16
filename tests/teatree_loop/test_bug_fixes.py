"""RED tests for four confirmed bugs — written before fixes to verify reproduction.

M1 — _sweep_white_check_mark crosses overlay boundaries (missing overlay= filter)
M4 — _fetch_review_state raises TypeError when get_draft_notes_count returns None
M6 — _decode_pr collapses missing/None number to pr_id=0, poisoning the marker table
L1 — _last_review_completed_at counts FAILED tasks, suppressing cadence for a week
"""

import json
from dataclasses import dataclass, field
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
        payload = json.loads(result.output.strip())
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


def _scanner_l1(*, cadence_hours: int = 1) -> ArchitecturalReviewScanner:
    return ArchitecturalReviewScanner(
        overlay_name=OVERLAY_A,
        cadence_hours=cadence_hours,
        after_merge_count=999,
    )


def _last_review_task(overlay: str = OVERLAY_A) -> Task | None:
    return (
        Task.objects.filter(
            ticket__overlay=overlay,
            phase=ARCHITECTURAL_REVIEW_PHASE,
        )
        .order_by("-id")
        .first()
    )


class TestL1FailedTaskDoesNotAdvanceCadence(TestCase):
    """A FAILED architectural-review task must NOT count as "last completed"."""

    def test_failed_task_does_not_suppress_next_dispatch(self) -> None:
        # Create a review task and mark it FAILED.
        signals = _scanner_l1(cadence_hours=1).scan()
        assert len(signals) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.FAILED)
        # Backdate to 2 hours ago — well past the 1-hour cadence window.
        Session.objects.filter(pk=prior.session_id).update(
            started_at=timezone.now() - __import__("datetime").timedelta(hours=2),
        )

        # With the bug: _last_review_completed_at sees the FAILED task's timestamp
        # (2 hours ago), cadence appears elapsed=2h >= 1h → a new task IS queued.
        # That coincidentally passes. The real invariant is the inverse:
        # a FAILED task less than cadence_hours ago should NOT suppress dispatch.
        # Seed a FAILED task that is only 30 minutes old.
        signals2 = _scanner_l1(cadence_hours=168).scan()
        assert len(signals2) == 1  # New task queued (prior was FAILED, not completed)
        prior2 = _last_review_task()
        assert prior2 is not None
        Task.objects.filter(pk=prior2.pk).update(status=Task.Status.FAILED)
        # 30 minutes ago — well inside cadence_hours=168.
        Session.objects.filter(pk=prior2.session_id).update(
            started_at=timezone.now() - __import__("datetime").timedelta(minutes=30),
        )

        # Bug: the query aggregates over ALL tasks (incl. FAILED), so it sees the
        # FAILED task 30min ago and treats cadence as not-elapsed → returns [].
        # Fix: only COMPLETED tasks count → cadence is elapsed from prior.pk (2h) →
        # a new task should be dispatched.
        signals3 = _scanner_l1(cadence_hours=168).scan()

        assert len(signals3) == 1, "FAILED task within cadence window incorrectly suppressed next dispatch"
