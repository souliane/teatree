"""DB-backed tests for ``ArchitecturalReviewScanner`` (#1136 / #1152).

The scanner periodically queues an ``architectural_review`` ``Task`` row for
each registered overlay using two independent triggers: a cadence (last
review older than ``architectural_review_cadence_hours``) and a
merge-count (``architectural_review_after_merge_count`` ticket merges
since the last queued review). The architectural review is a teatree-CORE
platform behaviour — it always applies uniformly to every overlay; the
only opt-out is the ``arch_review`` Loop row (or a preset masking it). The
cadence and skill are teatree-core config (DB-home ``ConfigSetting`` rows,
per-overlay overridable); the scanner itself always scans when invoked.

Integration-style with real Django ORM rows. Times are backdated with
``QuerySet.update()`` so we avoid an extra dep on a time-travel library
(mirrors :mod:`tests.teatree_loop.test_stale_tickets`).
"""

import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.config import UserSettings
from teatree.core.admission_governor import MachineSignal, QuotaSignal
from teatree.core.agent_admission import AgentAdmission, agent_admission_verdict
from teatree.core.models import PullRequest
from teatree.core.models.bot_ping import BotPing
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.owner_dm.architectural_review_notice import announce_held
from teatree.loop.scanners.architectural_review import ARCHITECTURAL_REVIEW_PHASE, ArchitecturalReviewScanner
from teatree.loop.transient_requeue import requeue_transient_failed
from teatree.provisioning.declared import project_root_for_running_code, skills_declared_in_apm_manifest
from teatree.skill_support.ref_validator import canonical_skill_names, default_search_dirs

OVERLAY = "acme"


@pytest.fixture
def mandated_skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the search dirs to the skills ``apm.yml`` MANDATES, not the operator's home.

    Unpinned, ``default_search_dirs`` reaches ``~/.claude/skills``, so the verdict is
    whatever the box happens to have installed — green on a provisioned machine, red on
    a runner that never ran ``apm install``. The mandate is the honest set and the one
    ``t3 setup`` provisions, so a name in neither the manifest nor the repo tree is the
    #3353 drift and nothing else is.
    """
    staged = tmp_path / "mandated"
    staged.mkdir()
    root = project_root_for_running_code()
    assert root is not None, "the running code's own checkout must be locatable"
    for dependency in skills_declared_in_apm_manifest(root / "apm.yml"):
        (staged / dependency.name).mkdir(parents=True, exist_ok=True)
        (staged / dependency.name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", f"{root / 'skills'}{os.pathsep}{staged}")
    return staged


@pytest.mark.usefixtures("mandated_skills_dir")
class TestDefaultSkillResolvesToARealSkill:
    """Regression for #3353: the default review skill must actually exist.

    ``ArchitecturalReviewScanner.skill`` and ``UserSettings.architectural_review_skill``
    both default to a skill name that every queued review task's ``execution_reason``
    names as the guidance to load. Neither ``t3 tool validate-skill-refs`` nor the
    antipattern-catalog ``consumers:`` field ever checked that this particular
    reference site resolves, so the name drifted to a directory that was never
    created — every periodic review ran with zero skill guidance. This asserts the
    default resolves against the same canonical skill set the skill-loading hook
    itself uses, so a future rename/typo here fails loudly instead of silently.
    """

    def test_scanner_default_skill_is_canonical(self) -> None:
        default_skill = ArchitecturalReviewScanner(overlay_name=OVERLAY).skill
        assert default_skill in canonical_skill_names(default_search_dirs())

    def test_settings_default_skill_is_canonical(self) -> None:
        default_skill = UserSettings().architectural_review_skill
        assert default_skill in canonical_skill_names(default_search_dirs())

    def test_a_name_nothing_mandates_is_flagged(self) -> None:
        # Anti-vacuity: the staged set is the mandate, not a copy of the answer.
        assert "ac-reviewing-skills" not in canonical_skill_names(default_search_dirs())


def _scanner(
    *,
    skill: str = "architectural-review",
    cadence_hours: int = 168,
    after_merge_count: int = 25,
) -> ArchitecturalReviewScanner:
    return ArchitecturalReviewScanner(
        overlay_name=OVERLAY,
        skill=skill,
        cadence_hours=cadence_hours,
        after_merge_count=after_merge_count,
    )


def _last_review_task(overlay: str = OVERLAY) -> Task | None:
    return (
        Task.objects.filter(
            ticket__overlay=overlay,
            phase=ARCHITECTURAL_REVIEW_PHASE,
        )
        .order_by("-id")
        .first()
    )


def _backdate_task(task: Task, *, hours: int) -> None:
    """Move a Task's bookkeeping into the past so the cadence math triggers.

    ``Task`` now has a ``created_at`` (migration 0004), but the scanner
    intentionally keys on ``Session.started_at`` (auto_now_add) as the queue
    time, so we derive the last-review timestamp from there and backdate the
    Session row via ``update()``.
    """
    Session.objects.filter(pk=task.session_id).update(
        started_at=timezone.now() - timedelta(hours=hours),
    )


def _make_merge_after(overlay: str, *, after_hours: int) -> Ticket:
    """Create a merged-state ticket with a transition timestamp ``after_hours`` ago.

    The scanner counts merged/delivered tickets whose latest matching
    TicketTransition is *after* the last review task. We backdate the
    transition row's ``created_at`` to control the test ordering.
    """
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://example.com/issues/{Ticket.objects.count() + 100}",
        state=Ticket.State.MERGED,
    )
    transition = TicketTransition.objects.create(
        ticket=ticket,
        from_state=Ticket.State.PR_OPENED,
        to_state=Ticket.State.MERGED,
    )
    TicketTransition.objects.filter(pk=transition.pk).update(
        created_at=timezone.now() - timedelta(hours=after_hours),
    )
    return ticket


def _seed_terminal_review(status: Task.Status, *, hours_ago: float, overlay: str = OVERLAY) -> Task:
    """Seed a terminal review task on the overlay's placeholder ticket, aged ``hours_ago``."""
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


def _queued_review_from_scan() -> Task:
    quota = QuotaSignal(
        fresh=True,
        all_accounts_exhausted=False,
        weekly_utilization=0.1,
        short_utilization=0.1,
        seconds_to_weekly_reset=7 * 24 * 3600,
    )
    with (
        patch("teatree.core.agent_admission.read_quota_signal", return_value=quota),
        patch(
            "teatree.core.agent_admission.read_machine_signal",
            return_value=MachineSignal(cores=8, load1=1, ram_available_gb=20),
        ),
        patch.object(Ticket, "has_dispatchable_overlay", return_value=True),
        patch("teatree.core.task_dispatch.enqueue_execution"),
    ):
        assert [signal.kind for signal in _scanner().scan()] == ["architectural_review.queued"]
    task = _last_review_task()
    assert task is not None
    return task


class ArchitecturalReviewScannerTests(TestCase):
    def test_scanned_review_completion_notifies_once_with_pr_link(self) -> None:
        task = _queued_review_from_scan()
        task.claim(claimed_by="test")
        url = "https://github.com/example-org/example-repo/pull/900"

        record_result_envelope(task, {"summary": f"Opened {url}"}, phase=task.phase)

        notes = BotPing.objects.filter(idempotency_key=f"architectural-review:completed:{task.pk}")
        assert notes.count() == 1
        assert url in notes.get().text

    def test_scanned_review_terminal_failure_notifies_once(self) -> None:
        task = _queued_review_from_scan()
        task.claim(claimed_by="test")
        task.fail(reason="credential available: none", by_holder=True)
        Task.objects.filter(pk=task.pk).update(execution_reason="[plans-only-retry]")
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error="credential available: none")

        with self.captureOnCommitCallbacks(execute=True):
            assert requeue_transient_failed() == 0
            assert requeue_transient_failed() == 0

        notes = BotPing.objects.filter(idempotency_key=f"architectural-review:failed:{task.pk}")
        assert notes.count() == 1
        assert "No credential configured" in notes.get().text

    def test_due_review_is_seated_at_creation_beside_full_coding_lane(self) -> None:
        quota = QuotaSignal(
            fresh=True,
            all_accounts_exhausted=False,
            weekly_utilization=0.1,
            short_utilization=0.1,
            seconds_to_weekly_reset=7 * 24 * 3600,
        )
        machine = MachineSignal(cores=8, load1=1, ram_available_gb=20)
        with (
            patch("teatree.core.agent_admission.read_quota_signal", return_value=quota),
            patch("teatree.core.agent_admission.read_machine_signal", return_value=machine),
            patch.object(Ticket, "has_dispatchable_overlay", return_value=True),
            patch("teatree.core.task_dispatch.enqueue_execution") as enqueue,
        ):
            width = agent_admission_verdict().expensive_lane.ceiling
            assert width is not None
            ticket = Ticket.objects.create(overlay=OVERLAY)
            session = Session.objects.create(ticket=ticket, overlay=OVERLAY)
            for _ in range(width):
                Task.objects.create(
                    ticket=ticket,
                    session=session,
                    phase="coding",
                    status=Task.Status.CLAIMED,
                    lease_expires_at=timezone.now() + timedelta(hours=1),
                )
            signals = _scanner().scan()

        assert [signal.kind for signal in signals] == ["architectural_review.queued"]
        review = _last_review_task()
        assert review is not None
        assert review.admitted_at is not None
        enqueue.assert_called_once_with(review.pk, "architectural_review")

    def test_due_review_queues_with_twenty_live_pull_requests(self) -> None:
        for index in range(20):
            PullRequest.objects.record_opened(
                ticket=Ticket.objects.create(),
                url=f"https://github.com/example-org/example-repo/pull/{index + 1600}",
            )

        signals = _scanner().scan()
        assert [signal.kind for signal in signals] == ["architectural_review.queued"]
        assert _last_review_task() is not None

    def test_admission_hold_emits_reason_and_one_owner_notice(self) -> None:
        denied = AgentAdmission(
            expensive_denied="load HALT",
            cheap_denied="load HALT",
            review_denied="load HALT",
            retiring_denied="load HALT",
            architectural_review_denied="load HALT",
        )
        with patch("teatree.core.agent_admission.agent_admission_verdict", return_value=denied):
            first = _scanner().scan()
            second = _scanner().scan()
        assert first[0].kind == "architectural_review.held"
        assert first[0].payload["reason"] == "load HALT"
        assert second[0].kind == "architectural_review.held"
        assert BotPing.objects.filter(idempotency_key__startswith="architectural-review:held:").count() == 1
        assert KnownIssue.objects.open().filter(fingerprint=f"architectural-review-held:{OVERLAY}").exists()

        Task.objects.filter(pk=first[0].payload["task_id"]).update(admitted_at=timezone.now())
        assert _scanner().scan() == []
        assert not KnownIssue.objects.open().filter(fingerprint=f"architectural-review-held:{OVERLAY}").exists()

    def test_lapsed_plans_retry_reports_governor_hold(self) -> None:
        task = _queued_review_from_scan()
        task.claim(claimed_by="test")
        task.fail(reason="api_credit: credit balance is too low", by_holder=True)
        TaskAttempt.objects.create(
            task=task, ended_at=timezone.now(), exit_code=1, error="api_credit: credit balance is too low"
        )
        assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert "[plans-only-retry]" in task.execution_reason
        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now() - timedelta(minutes=6))
        denied = AgentAdmission(
            expensive_denied=None,
            cheap_denied=None,
            review_denied=None,
            retiring_denied=None,
            architectural_review_denied="load HALT",
        )

        with patch("teatree.core.agent_admission.agent_admission_verdict", return_value=denied):
            signals = _scanner().scan()

        assert [signal.kind for signal in signals] == ["architectural_review.held"]
        assert signals[0].payload == {"overlay": OVERLAY, "task_id": task.pk, "reason": "load HALT"}
        assert BotPing.objects.filter(idempotency_key=f"architectural-review:held:{task.pk}:admission").count() == 1
        assert KnownIssue.objects.open().filter(fingerprint=f"architectural-review-held:{OVERLAY}").exists()

    def test_admission_and_owner_answer_holds_have_distinct_receipts(self) -> None:
        task = _queued_review_from_scan()
        announce_held(task, "load HALT", hold_kind="admission")
        announce_held(task, "awaiting the owner's answer", hold_kind="owner_answer")
        announce_held(task, "load HALT", hold_kind="admission")
        assert BotPing.objects.filter(idempotency_key__startswith=f"architectural-review:held:{task.pk}:").count() == 2

    def test_masked_dispatch_reports_held_instead_of_queued(self) -> None:
        allowed = AgentAdmission(
            expensive_denied=None,
            cheap_denied=None,
            review_denied=None,
            retiring_denied=None,
        )
        with (
            patch("teatree.core.admission.dispatch_mask.loop_admits", return_value=False),
            patch("teatree.core.agent_admission.agent_admission_verdict", return_value=allowed),
            patch.object(Ticket, "has_dispatchable_overlay", return_value=True),
        ):
            signals = _scanner().scan()

        assert [signal.kind for signal in signals] == ["architectural_review.held"]
        assert "dispatch loop is masked off" in signals[0].payload["reason"]
        task = _last_review_task()
        assert task is not None
        assert task.admitted_at is None

    def test_no_overlay_name_queues_nothing(self) -> None:
        """Defensive: an empty overlay_name short-circuits to no-op.

        The wiring layer never passes an empty name, but the scanner is
        defensive so a misconstructed instance does not poison the DB.
        """
        signals = ArchitecturalReviewScanner(overlay_name="", cadence_hours=1).scan()
        assert signals == []
        assert _last_review_task() is None

    def test_no_prior_review_queues_task(self) -> None:
        """First-ever run on an overlay queues exactly one task.

        Fail-safe (#1136 RED CARD): when invoked and no prior review
        task exists, the cadence is trivially elapsed → a task MUST be
        queued. Absence is the bug.
        """
        signals = _scanner().scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "architectural_review.queued"
        assert signal.payload["overlay"] == OVERLAY
        assert signal.payload["skill"] == "architectural-review"
        assert signal.payload["phase"] == ARCHITECTURAL_REVIEW_PHASE

        task = _last_review_task()
        assert task is not None
        assert task.phase == ARCHITECTURAL_REVIEW_PHASE
        assert task.status == Task.Status.PENDING
        assert task.ticket.overlay == OVERLAY

    def test_cadence_elapsed_queues_new_task(self) -> None:
        """A prior review older than cadence_hours triggers a new task."""
        # Seed a completed review task ``8 days`` ago.
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=24 * 8)

        second = _scanner(cadence_hours=168).scan()

        assert len(second) == 1
        task = _last_review_task()
        assert task is not None
        assert task.pk != prior.pk

    def test_cadence_not_elapsed_no_task(self) -> None:
        """A recent review within the cadence window blocks new queueing."""
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        # 13h ago — past the 12h backoff (so backoff isn't the reason) but far
        # inside the 168-hour success window, which is what suppresses here.
        _backdate_task(prior, hours=13)

        second = _scanner(cadence_hours=168).scan()

        assert [signal.kind for signal in second] == ["architectural_review.not_due"]
        completed_at = Session.objects.get(pk=prior.session_id).started_at
        assert second[0].payload["next_due_at"] == (completed_at + timedelta(hours=168)).isoformat()
        # No new task created.
        latest = _last_review_task()
        assert latest is not None
        assert latest.pk == prior.pk

    def test_pending_task_blocks_new_queueing(self) -> None:
        """A still-PENDING review task suppresses dupes even after cadence elapses."""
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        # Leave it PENDING and backdate so cadence WOULD trigger.
        _backdate_task(prior, hours=24 * 14)

        second = _scanner(cadence_hours=168).scan()

        assert second == []
        latest = _last_review_task()
        assert latest is not None
        assert latest.pk == prior.pk
        assert latest.status == Task.Status.PENDING

    def test_claimed_task_blocks_new_queueing(self) -> None:
        """A CLAIMED (in-flight) review task is treated as pending → no dupes."""
        _scanner(cadence_hours=168).scan()
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.CLAIMED)
        _backdate_task(prior, hours=24 * 14)

        second = _scanner(cadence_hours=168).scan()

        assert second == []

    def test_merge_count_trigger_fires(self) -> None:
        """3 merges since the last review with after_merge_count=2 → queue."""
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        # 13h ago — past the 12h backoff (so the backstop is free to fire) and
        # far inside the 999h cadence window (so cadence will not fire).
        _backdate_task(prior, hours=13)

        # Three merges after the prior review (transition timestamps "now").
        for _ in range(3):
            _make_merge_after(OVERLAY, after_hours=0)

        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert len(second) == 1
        # Cadence is not elapsed; only the merge-count trigger could have fired.
        assert second[0].payload["trigger"] == "after_merge_count"

    def test_merge_count_below_threshold_no_task(self) -> None:
        """One merge with after_merge_count=2 → no task (cadence also not elapsed)."""
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        # 13h ago — past the 12h backoff, so the threshold (not the backoff) is
        # what suppresses this one-merge case.
        _backdate_task(prior, hours=13)

        _make_merge_after(OVERLAY, after_hours=0)

        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert [signal.kind for signal in second] == ["architectural_review.not_due"]

    def test_a_recent_failure_does_not_suppress_the_cadence_trigger(self) -> None:
        """A FAILED attempt suppresses nothing — the cadence is the only clock.

        The inverse of the retired 12h post-failure backoff: a failed review leaves
        the COMPLETED clock untouched, so the next re-fire is the loop's own cadence.
        Re-introducing an attempt-recency gate turns this red.
        """
        _seed_terminal_review(Task.Status.FAILED, hours_ago=1)

        signals = _scanner().scan()

        assert len(signals) == 1
        assert signals[0].payload["trigger"] == "bootstrap"

    def test_a_recent_failure_does_not_suppress_the_merge_count_backstop(self) -> None:
        """Same for the backstop: a fresh FAILED attempt no longer gates it."""
        _seed_terminal_review(Task.Status.COMPLETED, hours_ago=13)
        for _ in range(3):
            _make_merge_after(OVERLAY, after_hours=0)
        _seed_terminal_review(Task.Status.FAILED, hours_ago=1)

        signals = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert len(signals) == 1
        assert signals[0].payload["trigger"] == "after_merge_count"

    def test_merge_count_backstop_fires(self) -> None:
        """The merge-count backstop fires past the last completed review."""
        _seed_terminal_review(Task.Status.COMPLETED, hours_ago=13)
        for _ in range(3):
            _make_merge_after(OVERLAY, after_hours=0)

        signals = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert len(signals) == 1
        assert signals[0].payload["trigger"] == "after_merge_count"

    def test_merge_count_ignores_merges_before_last_review(self) -> None:
        """Merges that happened *before* the last review don't count."""
        # An old merge in the books.
        _make_merge_after(OVERLAY, after_hours=24 * 30)

        # Seed and complete a recent review.
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=13)

        # Old merge predates the review — should not count.
        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert [signal.kind for signal in second] == ["architectural_review.not_due"]

    def test_overlay_isolation(self) -> None:
        """Merges in another overlay don't count toward this overlay's quota."""
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=13)

        # Three merges on a *different* overlay — must not count.
        for _ in range(3):
            _make_merge_after("other-overlay", after_hours=0)

        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert [signal.kind for signal in second] == ["architectural_review.not_due"]

    def test_queued_task_carries_skill_name(self) -> None:
        """The skill name lands in the Task's execution_reason for the dispatcher to pick up."""
        scanner = _scanner(skill="ac-custom-review")
        scanner.scan()

        task = _last_review_task()
        assert task is not None
        assert "ac-custom-review" in task.execution_reason

    def test_signal_summary_is_concise(self) -> None:
        """Statusline-friendly: one-line summary mentioning overlay + cadence reason."""
        signals = _scanner().scan()
        assert len(signals) == 1
        # No prior review → first-time trigger reason.
        assert OVERLAY in signals[0].summary


class ArchitecturalReviewWiringTests(TestCase):
    """Confirm the tick-job builder reads core config (#1136 / #1152).

    The architectural-review scanner is always-on for every registered
    overlay — the cadence + skill are teatree-core platform config, NOT
    a per-overlay opt-in. Turning the review off is the ``arch_review`` Loop row.
    """

    def _patched_settings(self, **overrides: object) -> UserSettings:
        """Build a UserSettings with the given overrides on top of defaults."""
        return UserSettings(**overrides)

    def test_default_core_config_builds_scanner(self) -> None:
        """Default core config → wiring produces a scanner.

        Anti-vacuousness: this used to require an explicit per-overlay
        opt-in on OverlayConfig. With the core re-architecture (#1152)
        the default core config alone suffices — no per-overlay opt-in
        needed.
        """
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.scanner_factories import _architectural_review_scanner_for  # noqa: PLC0415

        backend = OverlayBackends(name="acme", overlay=None)
        with patch(
            "teatree.loop.scanner_factories._effective_settings_for_overlay",
            return_value=self._patched_settings(),
        ):
            scanner = _architectural_review_scanner_for(backend)
        assert scanner is not None
        assert scanner.overlay_name == "acme"
        assert scanner.skill == "architectural-review"
        assert scanner.cadence_hours == 168
        assert scanner.after_merge_count == 25

    def test_core_config_propagates_to_scanner_kwargs(self) -> None:
        """Tuned core config flows through to the scanner kwargs."""
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.scanner_factories import _architectural_review_scanner_for  # noqa: PLC0415

        backend = OverlayBackends(name="acme", overlay=None)
        with patch(
            "teatree.loop.scanner_factories._effective_settings_for_overlay",
            return_value=self._patched_settings(
                architectural_review_skill="ac-custom",
                architectural_review_cadence_hours=72,
                architectural_review_after_merge_count=10,
            ),
        ):
            scanner = _architectural_review_scanner_for(backend)
        assert scanner is not None
        assert scanner.overlay_name == "acme"
        assert scanner.skill == "ac-custom"
        assert scanner.cadence_hours == 72
        assert scanner.after_merge_count == 10

    def test_overlay_without_python_class_still_wires(self) -> None:
        """TOML-only overlay (no Python class) gets a scanner now.

        The previous wiring skipped overlays with ``backend.overlay is
        None`` because it had to read OverlayConfig. With core-config
        sourcing, the scanner only needs ``backend.name`` — TOML-only
        overlays participate in the core platform cadence too.
        """
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.scanner_factories import _architectural_review_scanner_for  # noqa: PLC0415

        backend = OverlayBackends(name="acme", overlay=None)
        with patch(
            "teatree.loop.scanner_factories._effective_settings_for_overlay",
            return_value=self._patched_settings(),
        ):
            scanner = _architectural_review_scanner_for(backend)
        assert scanner is not None
