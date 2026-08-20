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
from teatree.core.models.sweep_skip_streak import (
    _CI_VERDICT_REASONS,
    DELIBERATE_PARK_REASONS,
    SKIP_REASON_DISPOSITION,
    SkipDisposition,
    disposition_for,
)
from teatree.loop.scanners import pr_sweep, pr_sweep_decision


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
        # An announcing reason, so the cooldown is what suppresses this — not the park class.
        for _ in range(3):
            _observe(reason="changes_requested")

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


_NOW = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)


def _streak(*, slug: str = "o/r", pr_id: int = 7, last_seen: dt.datetime = _NOW) -> SweepSkipStreak:
    return SweepSkipStreak.objects.create(
        slug=slug,
        pr_id=pr_id,
        reason="ci_pending",
        first_seen_at=last_seen,
        last_seen_at=last_seen,
        tick_count=57,
    )


class TestDropTerminal(django.test.TestCase):
    def test_a_named_pr_is_dropped_and_an_unnamed_one_kept(self) -> None:
        _streak(pr_id=7)
        _streak(pr_id=8)

        assert SweepSkipStreak.objects.drop_terminal(terminal_refs=[("o/r", 7)]) == 1
        assert list(SweepSkipStreak.objects.values_list("pr_id", flat=True)) == [8]

    def test_the_slug_match_folds_case_both_ways(self) -> None:
        _streak(slug="Owner/Repo")

        assert SweepSkipStreak.objects.drop_terminal(terminal_refs=[("owner/repo", 7)]) == 1

    def test_an_empty_ref_set_drops_nothing(self) -> None:
        _streak()

        assert SweepSkipStreak.objects.drop_terminal(terminal_refs=[]) == 0
        assert SweepSkipStreak.objects.count() == 1

    def test_a_same_numbered_pr_in_another_repo_is_untouched(self) -> None:
        _streak(slug="other/repo")

        assert SweepSkipStreak.objects.drop_terminal(terminal_refs=[("o/r", 7)]) == 0


class TestDropDeparted(django.test.TestCase):
    def test_a_row_unseen_past_the_boundary_is_dropped(self) -> None:
        _streak(last_seen=_NOW - dt.timedelta(hours=2))

        assert SweepSkipStreak.objects.drop_departed(slugs=["o/r"], stale_before=_NOW) == 1

    def test_a_row_seen_exactly_at_the_boundary_survives(self) -> None:
        _streak(last_seen=_NOW)

        assert SweepSkipStreak.objects.drop_departed(slugs=["o/r"], stale_before=_NOW) == 0
        assert SweepSkipStreak.objects.count() == 1

    def test_a_stale_row_in_an_unswept_slug_survives(self) -> None:
        _streak(slug="other/repo", last_seen=_NOW - dt.timedelta(days=15))

        assert SweepSkipStreak.objects.drop_departed(slugs=["o/r"], stale_before=_NOW) == 0

    def test_no_slug_swept_drops_nothing(self) -> None:
        _streak(last_seen=_NOW - dt.timedelta(days=15))

        assert SweepSkipStreak.objects.drop_departed(slugs=[], stale_before=_NOW) == 0
        assert SweepSkipStreak.objects.count() == 1

    def test_the_swept_slug_match_folds_case(self) -> None:
        _streak(slug="Owner/Repo", last_seen=_NOW - dt.timedelta(hours=2))

        assert SweepSkipStreak.objects.drop_departed(slugs=["owner/repo"], stale_before=_NOW) == 1


class TestDueToSurfaceIsBoundedByThisPass(django.test.TestCase):
    def _due(self, observed_since: dt.datetime | None) -> list[int]:
        rows = SweepSkipStreak.objects.due_to_surface(
            threshold=3,
            cooldown=dt.timedelta(hours=24),
            now=_NOW,
            observed_since=observed_since,
        )
        return [row.pr_id for row in rows]

    def test_a_row_this_pass_observed_is_due(self) -> None:
        _streak(last_seen=_NOW)

        assert self._due(_NOW) == [7]

    def test_a_row_this_pass_never_touched_is_not_due(self) -> None:
        _streak(last_seen=_NOW - dt.timedelta(minutes=1))

        assert self._due(_NOW) == []

    def test_an_unbounded_call_still_returns_it(self) -> None:
        _streak(last_seen=_NOW - dt.timedelta(days=15))

        assert self._due(None) == [7]


class TestLink(django.test.SimpleTestCase):
    def test_a_recorded_url_is_the_link(self) -> None:
        assert SweepSkipStreak(slug="o/r", pr_id=7, url="https://e.test/o/r/pull/7").link == "https://e.test/o/r/pull/7"

    def test_a_url_less_row_falls_back_to_the_ref(self) -> None:
        assert SweepSkipStreak(slug="o/r", pr_id=7, url="").link == "o/r#7"


def _scanner_skip_reasons() -> set[str]:
    """Every skip reason ``pr_sweep`` itself can emit, read from its own source."""
    tree = ast.parse(pathlib.Path(pr_sweep.__file__).read_text(encoding="utf-8"))
    reasons: set[str] = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not (isinstance(call.func, ast.Name) and call.func.id == "_skip"):
            continue
        reasons |= {
            kw.value.value
            for kw in call.keywords
            if kw.arg == "reason" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
        }
    precondition = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_precondition_skip_reason"
    )
    for returned in (node.value for node in ast.walk(precondition) if isinstance(node, ast.Return)):
        branches = [returned.body, returned.orelse] if isinstance(returned, ast.IfExp) else [returned]
        reasons |= {b.value for b in branches if isinstance(b, ast.Constant) and isinstance(b.value, str)}
    return reasons


class TestEverySkipReasonIsClassified(django.test.SimpleTestCase):
    """The whole point of the map: a new skip reason cannot join the alarm set unclassified."""

    def test_the_map_is_exactly_the_vocabulary_the_sweep_emits(self) -> None:
        emitted = _scanner_skip_reasons() | _classifier_skip_reasons()

        assert emitted == set(SKIP_REASON_DISPOSITION)

    def test_draft_is_the_deliberate_park(self) -> None:
        assert disposition_for("draft") is SkipDisposition.DELIBERATE_PARK
        assert {"draft"} == DELIBERATE_PARK_REASONS

    def test_a_ci_verdict_is_a_stall(self) -> None:
        assert disposition_for("ci_red") is SkipDisposition.STALL

    def test_an_unclassified_reason_still_alarms(self) -> None:
        assert disposition_for("invented_tomorrow") is SkipDisposition.STALL


class TestDeliberateParksNeverBecomeDue(django.test.TestCase):
    def test_a_park_at_the_threshold_is_not_due(self) -> None:
        for _ in range(3):
            _observe(reason="draft")

        assert list(SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)) == []

    def test_a_park_still_stands_in_the_doctor_view(self) -> None:
        for _ in range(3):
            _observe(reason="draft")

        assert [row.pr_id for row in SweepSkipStreak.objects.aged(threshold=3)] == [7]

    def test_a_stall_beside_a_park_is_still_due(self) -> None:
        for _ in range(3):
            _observe(reason="draft")
            _observe(pr_id=8, reason="ci_red")

        due = SweepSkipStreak.objects.due_to_surface(threshold=3, cooldown=_COOLDOWN)
        assert [row.pr_id for row in due] == [8]


class TestStanding(django.test.TestCase):
    def test_an_empty_ledger_stands_at_zero(self) -> None:
        summary = SweepSkipStreak.objects.standing(threshold=3)

        assert (summary.total, summary.stalls, summary.parks, summary.worst) == (0, 0, 0, ())

    def test_parks_and_stalls_are_counted_apart(self) -> None:
        for _ in range(3):
            _observe(reason="draft")
            _observe(pr_id=8, reason="ci_red")
            _observe(pr_id=9, reason="no_clear_for_head")

        summary = SweepSkipStreak.objects.standing(threshold=3)

        assert (summary.total, summary.stalls, summary.parks) == (3, 2, 1)

    def test_below_the_threshold_never_stands(self) -> None:
        _observe(reason="ci_red")

        assert SweepSkipStreak.objects.standing(threshold=3).total == 0

    def test_the_detail_names_only_stalls_oldest_first_and_is_capped(self) -> None:
        start = timezone.now() - dt.timedelta(hours=10)
        _observe(reason="draft", now=start)
        for offset, pr_id in enumerate((8, 9, 10, 11), start=1):
            _observe(pr_id=pr_id, reason="ci_red", now=start + dt.timedelta(hours=offset))
        for _ in range(2):
            for pr_id in (7, 8, 9, 10, 11):
                _observe(pr_id=pr_id, reason="draft" if pr_id == 7 else "ci_red")

        summary = SweepSkipStreak.objects.standing(threshold=3, limit=3)

        assert [row.pr_id for row in summary.worst] == [8, 9, 10]

    def test_the_oldest_age_spans_parks_too(self) -> None:
        start = timezone.now() - dt.timedelta(hours=9)
        for _ in range(3):
            _observe(reason="draft", now=start)
            _observe(pr_id=8, reason="ci_red")

        assert SweepSkipStreak.objects.standing(threshold=3).oldest_age_label == "9h"
