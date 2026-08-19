"""Consecutive-skip ledger for the PR sweep — the durable half of aged-skip surfacing.

``pr_sweep`` skips on ~10 reasons, all log-only, so a PR can sit forever with nobody
told why. The ledger counts how many consecutive sweep passes produced the SAME skip
reason for a PR, so persistence — not any single skip — is what surfaces.
"""

import ast
import datetime as dt
import pathlib

import django.test
from django.utils import timezone

from teatree.core.models import SkipObservation, SweepSkipStreak
from teatree.core.models.sweep_skip_streak import _CI_VERDICT_REASONS
from teatree.loop.scanners import pr_sweep_decision


def _observe(
    *,
    pr_id: int = 7,
    reason: str = "ci_pending",
    url: str = "",
    overlay: str = "",
    now: dt.datetime | None = None,
) -> SweepSkipStreak:
    return SweepSkipStreak.objects.observe(
        SkipObservation(slug="o/r", pr_id=pr_id, reason=reason, url=url, overlay=overlay),
        now=now,
    )


class TestObserve(django.test.TestCase):
    def test_first_skip_opens_a_streak(self) -> None:
        row = _observe(url="u", overlay="t3")

        assert row.tick_count == 1
        assert row.surfaced_at is None
        assert row.reason == "ci_pending"
        assert row.url == "u"

    def test_the_same_reason_accumulates(self) -> None:
        for _ in range(3):
            _observe()

        assert SweepSkipStreak.objects.get(slug="o/r", pr_id=7).tick_count == 3

    def test_a_different_reason_restarts_the_streak(self) -> None:
        _observe()
        _observe()
        row = _observe(reason="draft")

        assert row.reason == "draft"
        assert row.tick_count == 1

    def test_a_new_reason_shortly_after_surfacing_does_not_clear_the_cooldown(self) -> None:
        for _ in range(3):
            _observe()
        SweepSkipStreak.objects.mark_surfaced([SweepSkipStreak.objects.get(slug="o/r", pr_id=7).pk])
        _observe(reason="draft")

        row = SweepSkipStreak.objects.get(slug="o/r", pr_id=7)
        assert row.reason == "draft"
        assert row.tick_count == 1
        assert row.surfaced_at is not None

    def test_distinct_prs_keep_distinct_streaks(self) -> None:
        _observe(pr_id=7)
        _observe(pr_id=8)

        assert SweepSkipStreak.objects.count() == 2


class TestResolve(django.test.TestCase):
    def test_resolve_drops_the_streak(self) -> None:
        _observe()

        assert SweepSkipStreak.objects.resolve(slug="o/r", pr_id=7) == 1
        assert not SweepSkipStreak.objects.filter(slug="o/r", pr_id=7).exists()

    def test_resolving_an_unknown_pr_is_a_no_op(self) -> None:
        assert SweepSkipStreak.objects.resolve(slug="o/r", pr_id=99) == 0


_COOLDOWN = dt.timedelta(hours=24)


class TestDueToSurface(django.test.TestCase):
    def test_below_the_threshold_nothing_is_due(self) -> None:
        _observe()

        assert list(SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)) == []

    def test_at_the_threshold_the_streak_is_due(self) -> None:
        for _ in range(3):
            _observe()

        due = SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)
        assert [row.pr_id for row in due] == [7]

    def test_an_already_surfaced_streak_is_not_due_again(self) -> None:
        for _ in range(4):
            _observe()
        SweepSkipStreak.objects.mark_surfaced([SweepSkipStreak.objects.get(slug="o/r", pr_id=7).pk])
        _observe()

        assert list(SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)) == []

    def test_a_reason_change_within_the_cooldown_does_not_re_arm(self) -> None:
        for _ in range(3):
            _observe()
        SweepSkipStreak.objects.mark_surfaced([SweepSkipStreak.objects.get(slug="o/r", pr_id=7).pk])
        for _ in range(3):
            _observe(reason="draft")

        assert list(SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)) == []

    def test_a_streak_is_due_again_once_the_cooldown_has_elapsed(self) -> None:
        for _ in range(3):
            _observe()
        row = SweepSkipStreak.objects.get(slug="o/r", pr_id=7)
        stale_surface = timezone.now() - _COOLDOWN - dt.timedelta(minutes=1)
        SweepSkipStreak.objects.filter(pk=row.pk).update(surfaced_at=stale_surface)

        due = SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)
        assert [r.pr_id for r in due] == [7]

    def test_a_streak_surfaced_just_within_the_cooldown_boundary_is_not_due(self) -> None:
        for _ in range(3):
            _observe()
        row = SweepSkipStreak.objects.get(slug="o/r", pr_id=7)
        recent_surface = timezone.now() - _COOLDOWN + dt.timedelta(minutes=1)
        SweepSkipStreak.objects.filter(pk=row.pk).update(surfaced_at=recent_surface)

        assert list(SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)) == []


class TestAged(django.test.TestCase):
    def test_aged_reports_regardless_of_having_been_surfaced(self) -> None:
        for _ in range(3):
            _observe()
        SweepSkipStreak.objects.mark_surfaced([SweepSkipStreak.objects.get(slug="o/r", pr_id=7).pk])

        assert [row.pr_id for row in SweepSkipStreak.objects.aged(threshold=3)] == [7]

    def test_age_is_measured_from_the_first_sighting(self) -> None:
        started = timezone.now() - dt.timedelta(hours=5)
        row = _observe()
        SweepSkipStreak.objects.filter(pk=row.pk).update(first_seen_at=started)
        stored = SweepSkipStreak.objects.get(pk=row.pk)

        assert stored.age_label(now=started + dt.timedelta(hours=5)) == "5h"
        assert stored.age(now=started + dt.timedelta(hours=5)) == dt.timedelta(hours=5)


class TestRendering(django.test.TestCase):
    def test_str_names_the_pr_reason_and_run_length(self) -> None:
        row = _observe()

        assert str(row) == "sweep-skip<o/r#7 ci_pending x1>"


class TestCiVerdictGroupContinuity(django.test.TestCase):
    """One PR whose checks are not clean yet is ONE condition, however its verdict flaps.

    ``classify_sweep_ci`` emits four reasons for that single condition, and a PR's checks
    legitimately alternate between them on consecutive passes. Restarting the run length on
    each flip left the flappiest PRs — the ones most worth announcing — permanently below
    the surface threshold (souliane/teatree#4095).
    """

    def test_a_flip_within_the_ci_group_continues_the_streak(self) -> None:
        _observe(reason="ci_pending")
        _observe(reason="required_checks_indeterminate")
        row = _observe(reason="ci_red")

        assert row.tick_count == 3
        assert row.reason == "ci_red"

    def test_the_uv_audit_fallback_shares_the_group(self) -> None:
        _observe(reason="ci_red")
        row = _observe(reason="uv_audit_red_but_clean_on_main")

        assert row.tick_count == 2

    def test_the_reported_age_runs_from_the_groups_start(self) -> None:
        start = timezone.now() - dt.timedelta(hours=4)
        _observe(reason="ci_pending", now=start)
        row = _observe(reason="ci_red", now=start + dt.timedelta(hours=4))

        assert row.first_seen_at == start
        assert row.age_label(now=start + dt.timedelta(hours=4)) == "4h"

    def test_a_flip_within_the_group_does_not_re_arm_a_surfaced_streak(self) -> None:
        for _ in range(3):
            _observe(reason="ci_red")
        SweepSkipStreak.objects.mark_surfaced([SweepSkipStreak.objects.get(slug="o/r", pr_id=7).pk])
        row = _observe(reason="ci_pending")

        assert row.surfaced_at is not None
        assert list(SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)) == []

    def test_a_non_ci_reason_still_restarts_the_streak(self) -> None:
        _observe(reason="ci_pending")
        _observe(reason="ci_red")
        row = _observe(reason="changes_requested")

        assert row.tick_count == 1
        assert row.reason == "changes_requested"


def _classifier_skip_reasons() -> set[str]:
    """Every skip reason ``classify_sweep_ci`` can return, read from its own source."""
    source = pathlib.Path(pr_sweep_decision.__file__).read_text(encoding="utf-8")
    classifier = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "classify_sweep_ci"
    )
    returned = (node.value for node in ast.walk(classifier) if isinstance(node, ast.Return))
    verdicts = (value.elts[0] for value in returned if isinstance(value, ast.Tuple) and value.elts)
    return {node.value for node in verdicts if isinstance(node, ast.Constant) and isinstance(node.value, str)}


class TestCiVerdictGroupTracksTheClassifier(django.test.SimpleTestCase):
    def test_the_group_is_exactly_what_the_ci_classifier_emits(self) -> None:
        assert _classifier_skip_reasons() == set(_CI_VERDICT_REASONS)
