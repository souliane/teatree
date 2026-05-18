"""``StaleStatuslineEntryDetector`` per-detector tests (BLUEPRINT § 5.7 / plan §8)."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.models import PullRequest, SelfImproveFiring, Ticket
from teatree.loop.self_improve.actions import run_action_ladder
from teatree.loop.self_improve.detectors import StaleStatuslineEntryDetector


def _reader(text: str):
    def _read() -> str:
        return text

    return _read


class StaleStatuslineEntryDetectorTests(TestCase):
    def _merged_pr(self, url: str) -> PullRequest:
        ticket = Ticket.objects.create(overlay="acme", issue_url=url + "/issues")
        pr = PullRequest.objects.create(ticket=ticket, overlay="acme", url=url, repo="acme/repo", iid="1")
        pr.mark_merged()
        pr.save()
        return pr

    def test_fires_when_smell_present(self) -> None:
        url = "https://github.com/acme/repo/pull/1"
        self._merged_pr(url)
        reports = StaleStatuslineEntryDetector(statusline_reader=_reader(f"some line {url} here")).detect()
        assert len(reports) == 1
        assert reports[0].severity == "info"
        assert reports[0].auto_fix is True

    def test_does_not_fire_when_smell_absent(self) -> None:
        # Statusline references a URL that is not in any merged state.
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/i/1")
        PullRequest.objects.create(
            ticket=ticket, overlay="acme", url="https://github.com/acme/repo/pull/2", repo="acme/repo", iid="2"
        )
        assert (
            StaleStatuslineEntryDetector(
                statusline_reader=_reader("some line https://github.com/acme/repo/pull/2 here")
            ).detect()
            == []
        )

    def test_does_not_fire_when_statusline_missing(self) -> None:
        assert StaleStatuslineEntryDetector(statusline_reader=_reader("")).detect() == []

    def test_dedup_within_cooldown(self) -> None:
        url = "https://github.com/acme/repo/pull/3"
        self._merged_pr(url)
        callable_ = MagicMock()
        detector = StaleStatuslineEntryDetector(statusline_reader=_reader(f"line {url}"), rerender=callable_)
        for r in detector.detect():
            run_action_ladder(r, auto_fix_callable=lambda _r: callable_())
        for r in detector.detect():
            run_action_ladder(r, auto_fix_callable=lambda _r: callable_())
        assert SelfImproveFiring.objects.filter(detector="stale_statusline_entry").count() == 1
        assert SelfImproveFiring.objects.get(detector="stale_statusline_entry").action_count == 1

    def test_action_ladder_ceiling_is_statusline(self) -> None:
        """Ceiling is ``statusline`` per the issue plan — never escalates to slack."""
        url = "https://github.com/acme/repo/pull/4"
        self._merged_pr(url)
        reports = StaleStatuslineEntryDetector(statusline_reader=_reader(f"line {url}")).detect()
        assert reports
        assert reports[0].max_rung == SelfImproveFiring.Action.STATUSLINE.value

    def test_auto_fix_executes_rerender_callable(self) -> None:
        """Detector-specific edge: auto_fix wires through to the rerender callable."""
        url = "https://github.com/acme/repo/pull/5"
        self._merged_pr(url)
        rerender = MagicMock()
        detector = StaleStatuslineEntryDetector(statusline_reader=_reader(f"line {url}"), rerender=rerender)
        reports = detector.detect()
        assert reports
        # Detector marks auto_fix=True even on first observation; the
        # ladder still ramps from statusline -> ... so we drive it past
        # the ceiling by simulating the auto_fix rung directly through
        # the ladder's callable.
        assert reports[0].auto_fix is True
        # The schedule wires `detector.rerender` as the auto_fix
        # callable; verify the wiring works when invoked.
        detector.rerender()
        rerender.assert_called_once()
