"""Which checks snapshots admit a ``merge_safe`` verdict, decided against a live CI read (#4554).

The refusal itself never weakens: a ``merge_safe`` row may not carry ``failed`` whatever CI
says. What the live read decides is the CLASSIFICATION — only a red the forge itself confirms
is the terminal-eligible :class:`ChecksContradictionError`; a live green and a live
unreadable are ordinary refusals the next reviewer can get right.
"""

import pytest
from django.test import TestCase

from teatree.core.modelkit.forge_readability import LiveChecksProbe, LiveChecksRead
from teatree.core.models import ChecksContradictionError, ReviewVerdict, ReviewVerdictError

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "c" * 40
_SLUG = "souliane/teatree"
_PR_ID = 4554


class _CountingProbe:
    """A :class:`LiveChecksProbe` that records whether the refusal path reached it."""

    def __init__(self, reading: LiveChecksRead) -> None:
        self.reading = reading
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, slug: str, head_sha: str) -> LiveChecksRead:
        self.calls.append((slug, head_sha))
        return self.reading


def _red() -> _CountingProbe:
    return _CountingProbe(LiveChecksRead(status="failed", detail="failing workflow run(s): test (3.13)"))


def _green() -> _CountingProbe:
    return _CountingProbe(LiveChecksRead(status="green", detail="7 workflow run(s) concluded green"))


def _unreadable() -> _CountingProbe:
    return _CountingProbe(LiveChecksRead.unreadable("the workflow-run read failed (rc=1)"))


def _record(
    *,
    verdict: str = "merge_safe",
    gh_verify_result: str = "green",
    expedited: bool = False,
    live_checks: LiveChecksProbe | None = None,
) -> ReviewVerdict:
    return ReviewVerdict.record(
        pr_id=_PR_ID,
        slug=_SLUG,
        reviewed_sha=_SHA,
        verdict=verdict,
        reviewer_identity="cold-reviewer-agent",
        gh_verify_result=gh_verify_result,
        expedited=expedited,
        live_checks=live_checks,
    )


def _no_verdict_recorded() -> bool:
    return not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()


class TestLiveReadDecidesTheContradiction(TestCase):
    def test_a_live_confirmed_red_is_the_contradiction(self) -> None:
        probe = _red()

        with pytest.raises(ChecksContradictionError) as raised:
            _record(gh_verify_result="failed", live_checks=probe)

        assert probe.calls == [(_SLUG, _SHA)]
        assert "test (3.13)" in str(raised.value)
        assert _no_verdict_recorded()

    def test_a_live_green_refuses_without_being_a_contradiction(self) -> None:
        # The envelope misreported its own checks — a defect in ONE run, which the ordinary
        # retry already recovered on 6 of the 9 heads that ever hit this refusal.
        probe = _green()

        with pytest.raises(ReviewVerdictError) as raised:
            _record(gh_verify_result="failed", live_checks=probe)

        assert not isinstance(raised.value, ChecksContradictionError)
        assert "concluded green" in str(raised.value)
        assert _no_verdict_recorded()

    def test_an_unreadable_live_read_is_neither_admitted_nor_a_contradiction(self) -> None:
        probe = _unreadable()

        with pytest.raises(ReviewVerdictError) as raised:
            _record(gh_verify_result="failed", live_checks=probe)

        assert not isinstance(raised.value, ChecksContradictionError)
        assert "rc=1" in str(raised.value)
        assert _no_verdict_recorded()

    def test_no_probe_reads_as_unreadable_never_as_a_contradiction(self) -> None:
        with pytest.raises(ReviewVerdictError) as raised:
            _record(gh_verify_result="failed")

        assert not isinstance(raised.value, ChecksContradictionError)
        assert _no_verdict_recorded()

    def test_every_outcome_keeps_the_invariant_wording(self) -> None:
        for probe in (_red(), _green(), _unreadable()):
            with pytest.raises(ReviewVerdictError) as raised:
                _record(gh_verify_result="failed", live_checks=probe)

            assert "never carry gh_verify_result=failed" in str(raised.value)


class TestTheProbeStaysOffEveryOtherPath(TestCase):
    def test_a_hold_on_red_checks_records_without_reading_ci(self) -> None:
        probe = _red()

        recorded = _record(verdict="hold", gh_verify_result="failed", live_checks=probe)

        assert recorded.verdict == ReviewVerdict.Verdict.HOLD
        assert probe.calls == []

    def test_a_green_merge_safe_records_without_reading_ci(self) -> None:
        probe = _red()

        recorded = _record(live_checks=probe)

        assert recorded.is_merge_safe()
        assert probe.calls == []

    def test_pending_without_the_waiver_is_refused_without_reading_ci(self) -> None:
        probe = _red()

        with pytest.raises(ReviewVerdictError) as raised:
            _record(gh_verify_result="pending", live_checks=probe)

        assert not isinstance(raised.value, ChecksContradictionError)
        assert "expedite waiver" in str(raised.value)
        assert probe.calls == []

    def test_pending_with_the_waiver_records_without_reading_ci(self) -> None:
        probe = _red()

        recorded = _record(gh_verify_result="pending", expedited=True, live_checks=probe)

        assert recorded.is_merge_safe()
        assert probe.calls == []
