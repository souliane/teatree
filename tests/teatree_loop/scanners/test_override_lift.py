"""The override-lift scanner PROPOSES and never lifts (A8).

An override is a person's decision, so the factory's whole job is to notice that the
condition justifying it has cleared and ASK. That is only possible because an override
carries the REASON it was set — without one the only available policy is a timer, which
is exactly the auto-revert A6 was retracted for.

So the load-bearing assertions are what it does NOT do: it writes no override, it raises
nothing for a reason it cannot judge until that reason has aged, and it never asks the
owner to confirm the system's own bookkeeping. Integration-first against the real DB and
the real `DeferredQuestion` table.
"""

import datetime as dt
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import DeferredQuestion, Loop, Prompt, PullRequest, Ticket
from teatree.loop.scanners.override_lift import (
    PROSE_REMINDER_AGE,
    OverrideLiftScanner,
    override_lift_proposals,
    raise_override_lift_questions,
)

_NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)
_PR_URL = "https://example.test/org/repo/-/merge_requests/7"
_ISSUE_URL = "https://example.test/org/repo/-/issues/9"


def _loop(name: str, *, runs: bool, reason: str, age: dt.timedelta = dt.timedelta(0)) -> Loop:
    prompt, _ = Prompt.objects.get_or_create(name="override-lift", defaults={"body": "x"})
    Loop.objects.create(name=name, delay_seconds=60, prompt=prompt)
    Loop.objects.set_manual_override(name, runs=runs, reason=reason)
    Loop.objects.filter(name=name).update(override_set_at=_NOW - age)
    return Loop.objects.get(name=name)


class TestNothingIsProposedWithoutEvidence(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()

    def test_a_loop_with_no_override_is_never_proposed(self) -> None:
        prompt, _ = Prompt.objects.get_or_create(name="override-lift", defaults={"body": "x"})
        Loop.objects.create(name="plain", delay_seconds=60, prompt=prompt)

        assert override_lift_proposals(_NOW) == []

    def test_a_machine_checkable_reason_whose_condition_still_holds_is_not_proposed(self) -> None:
        _loop("held", runs=False, reason=f"pr:{_PR_URL}")

        assert override_lift_proposals(_NOW) == []

    def test_a_prose_reason_earns_nothing_until_it_has_aged(self) -> None:
        _loop("young", runs=False, reason="the connector is flapping", age=PROSE_REMINDER_AGE - dt.timedelta(hours=1))

        assert override_lift_proposals(_NOW) == []

    def test_the_systems_own_reason_is_never_raised_back_at_the_owner(self) -> None:
        # `auto:` reasons clear on their own signal; asking would be the factory asking
        # the owner to confirm its own bookkeeping.
        _loop("parked", runs=False, reason="auto:token-outage (usage window parked)", age=dt.timedelta(days=90))

        assert override_lift_proposals(_NOW) == []


class TestAClearedConditionIsProposed(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()

    def test_a_settled_pr_makes_its_override_liftable(self) -> None:
        ticket = Ticket.objects.create(overlay="t", issue_url=_ISSUE_URL)
        PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo="r", iid="7", state=PullRequest.State.MERGED)
        _loop("waiting", runs=False, reason=f"pr:{_PR_URL}")

        proposals = override_lift_proposals(_NOW)

        assert [p.loop_name for p in proposals] == ["waiting"]
        assert "now reads as resolved" in proposals[0].question

    def test_an_aged_prose_reason_earns_a_reminder_naming_how_long(self) -> None:
        _loop("standing", runs=True, reason="waiting on the vendor", age=dt.timedelta(days=30))

        proposals = override_lift_proposals(_NOW)

        assert [p.loop_name for p in proposals] == ["standing"]
        assert "30 days" in proposals[0].question
        assert "Nothing here can judge" in proposals[0].question

    def test_the_question_names_which_way_the_override_forces_the_loop(self) -> None:
        _loop("forced-on", runs=True, reason="incident", age=dt.timedelta(days=30))
        _loop("forced-off", runs=False, reason="incident", age=dt.timedelta(days=30))

        said = {p.loop_name: p.question for p in override_lift_proposals(_NOW)}

        assert "'forced-on' on" in said["forced-on"]
        assert "'forced-off' off" in said["forced-off"]


class TestProposingIsNotLifting(django.test.TestCase):
    """A4/A5: nothing auto-clears an override. The scanner asks; the owner decides."""

    def setUp(self) -> None:
        Loop.objects.all().delete()

    def test_the_override_is_untouched_by_a_full_scan(self) -> None:
        _loop("standing", runs=False, reason="waiting on the vendor", age=dt.timedelta(days=30))

        OverrideLiftScanner().scan()

        row = Loop.objects.get(name="standing")
        assert row.enabled is False, "the scanner must never lift an override"
        assert row.override_reason == "waiting on the vendor"

    def test_one_question_per_loop_however_many_passes_run(self) -> None:
        _loop("standing", runs=False, reason="waiting on the vendor", age=dt.timedelta(days=30))

        raise_override_lift_questions(_NOW)
        raise_override_lift_questions(_NOW)

        assert DeferredQuestion.objects.filter(dedupe_marker="override-lift:standing").count() == 1

    def test_the_scan_emits_one_signal_per_proposal(self) -> None:
        _loop("standing", runs=False, reason="waiting on the vendor", age=dt.timedelta(days=30))

        signals = OverrideLiftScanner().scan()

        assert [s.kind for s in signals] == ["override.lift_candidate"]
        assert signals[0].payload["loop"] == "standing"
        assert signals[0].payload["runs"] is False


class TestAFailedCheckKeepsTheOverride(django.test.TestCase):
    """An unjudgeable condition reads as STILL STANDING — never as cleared."""

    def setUp(self) -> None:
        Loop.objects.all().delete()

    def test_a_malformed_machine_reason_proposes_nothing(self) -> None:
        # `pr:` with no url: the checker runs, finds nothing, and the override stands
        # rather than being proposed for a lift on an unanswerable condition.
        _loop("broken", runs=False, reason="pr:", age=dt.timedelta(days=90))

        assert override_lift_proposals(_NOW) == []

    def test_an_unaged_machine_reason_is_judged_on_its_condition_not_its_age(self) -> None:
        ticket = Ticket.objects.create(overlay="t", issue_url=_ISSUE_URL)
        PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo="r", iid="7", state=PullRequest.State.MERGED)
        _loop("fresh", runs=False, reason=f"pr:{_PR_URL}", age=dt.timedelta(minutes=1))

        assert [p.loop_name for p in override_lift_proposals(_NOW)] == ["fresh"]


class TestTheScannerNeverCrashesTheTick(django.test.TestCase):
    def test_a_failing_read_degrades_to_no_signals(self) -> None:
        # Hosted on the hourly housekeeping loop, so a raise here would take the whole
        # pass with it — for a reminder.
        with patch(
            "teatree.loop.scanners.override_lift.override_lift_proposals",
            side_effect=RuntimeError("the control DB went away"),
        ):
            assert OverrideLiftScanner().scan() == []


class TestAgeIsMeasuredFromWhenTheOverrideWasSet(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()

    def test_an_override_with_no_set_at_is_never_aged_out(self) -> None:
        # A row that predates the column has no age to measure, and inventing one would
        # date the reminder from now — quietly restarting the clock on every upgrade.
        _loop("undated", runs=False, reason="waiting on the vendor")
        Loop.objects.filter(name="undated").update(override_set_at=None)

        assert override_lift_proposals(timezone.now()) == []
