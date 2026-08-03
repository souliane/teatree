"""Reviewer repro (scratch, not for commit) — false brake from stale streak rows."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.admission_governor import read_merge_signal
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import PullRequest, SweepSkipStreak
from teatree.loop.scanner_factories import _issue_intake_scanner_for
from tests.factories import TicketFactory

_PATCH_TARGET = "teatree.loop.scanner_factories._effective_settings_for_overlay"


def _backend(name: str = "acme") -> OverlayBackends:
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        identities=("alice",),
        overlay=None,
    )


def _enabled(**kw: object) -> UserSettings:
    return UserSettings(issue_implementer_enabled=True, user_identity_aliases=["alice"], **kw)


class StaleStreakRepro(TestCase):
    def test_stale_streaks_from_settled_prs_brake_a_healthy_pipeline(self) -> None:
        # 3 live PRs, all HEALTHY — none has ever been skipped.
        for i in range(3):
            t = TicketFactory(overlay="acme", issue_url=f"https://github.com/o/r/issues/{900 + i}")
            PullRequest.objects.create(
                ticket=t,
                overlay="acme",
                url=f"https://github.com/o/r/pull/{900 + i}",
                repo="o/r",
                iid=str(900 + i),
            )
        # 5 streak rows left behind by PRs settled long ago (merged/closed outside the
        # sweep). Nothing ever deletes them: `resolve` only fires on a live pr_sweep.*
        # signal for that exact (slug, pr_id).
        for i in range(5):
            SweepSkipStreak.objects.create(
                slug="o/r",
                pr_id=700 + i,
                reason="ci red",
                tick_count=9,
                overlay="acme",
            )
        signal = read_merge_signal()
        with patch(_PATCH_TARGET, return_value=_enabled()):
            scanner = _issue_intake_scanner_for(_backend())
        assert scanner is not None, "cross-overlay bleed: a stall in 'other' braked 'acme'"
        assert scanner.can_claim is True, "cross-overlay bleed: a stall in 'other' braked 'acme'"
        assert not signal.stalled, "false brake: healthy pipeline reported stalled"

    def test_a_stall_in_one_overlay_brakes_intake_in_another(self) -> None:
        for i in range(3):
            t = TicketFactory(overlay="other", issue_url=f"https://github.com/x/y/issues/{500 + i}")
            PullRequest.objects.create(
                ticket=t,
                overlay="other",
                url=f"https://github.com/x/y/pull/{500 + i}",
                repo="x/y",
                iid=str(500 + i),
            )
            SweepSkipStreak.objects.create(
                slug="x/y",
                pr_id=500 + i,
                reason="conflict",
                tick_count=9,
                overlay="other",
            )
        # 'acme' has no PRs at all and nothing stuck.
        with patch(_PATCH_TARGET, return_value=_enabled()):
            scanner = _issue_intake_scanner_for(_backend("acme"))
        braked = scanner is None or scanner.can_claim is False
        assert not braked, "cross-overlay bleed: a stall in 'other' braked 'acme'"
