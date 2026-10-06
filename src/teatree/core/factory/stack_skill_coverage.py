"""A recent dispatch on a Python/Django repo that ran without a stack skill its repo calls for.

The stack skills (``ac-django`` / ``ac-python`` / ``fastapi``) reach a headless agent only
when the dispatch detected them, and a detection that silently finds nothing leaves no
other trace: the bundle simply lacks them. This reads the bundles recent attempts RECORDED
(``TaskAttempt.skills_loaded``) against what each ticket's checkout or clone calls for, so
a regression in either the detection root or the bundle shows on the health chip.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models import Ticket
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.worktree.clone_paths import dispatch_detection_root
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

WINDOW_HOURS = 6
WINDOW = timedelta(hours=WINDOW_HOURS)
MAX_ATTEMPTS = 200
_NAMED_TASKS = 5


@dataclass(frozen=True, slots=True)
class StackSkillGap:
    task_id: int
    missing: frozenset[str]


def stack_skill_gaps() -> list[StackSkillGap]:
    """Newest first: each recent attempt whose recorded bundle lacks a stack skill its repo calls for."""
    expected: dict[int, frozenset[str]] = {}
    gaps: list[StackSkillGap] = []
    attempts = (
        TaskAttempt.objects.filter(started_at__gte=timezone.now() - WINDOW)
        .select_related("task__ticket")
        .order_by("-started_at", "-pk")[:MAX_ATTEMPTS]
    )
    for attempt in attempts:
        if not attempt.skills_loaded:
            continue
        ticket = attempt.task.ticket
        if ticket.pk not in expected:
            expected[ticket.pk] = _stack_skills_for(ticket)
        if missing := expected[ticket.pk] - set(attempt.skills_loaded):
            gaps.append(StackSkillGap(task_id=attempt.task.pk, missing=missing))
    return gaps


def _stack_skills_for(ticket: Ticket) -> frozenset[str]:
    root = dispatch_detection_root(ticket)
    return frozenset(SkillLoadingPolicy.detect_framework_skills(root)) if root is not None else frozenset()


def stack_skill_coverage_signals() -> SignalCollection:
    """One WARNING while a recent dispatch on a Python/Django repo lacked a stack skill; fail-open."""
    try:
        gaps = stack_skill_gaps()
    except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
        warn_throttled(logger, "health-stack-skill-coverage", "stack-skill coverage health read failed", exc_info=True)
        return SignalCollection(unread=("stack_skill_coverage_signals",))
    if not gaps:
        return SignalCollection()
    missing = sorted({name for gap in gaps for name in gap.missing})
    tasks = ", ".join(str(gap.task_id) for gap in gaps[:_NAMED_TASKS])
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="stack-skills-missing",
                severity=KnownIssue.Severity.WARNING,
                kind="stack_skills_missing",
                summary=(
                    f"{len(gaps)} dispatch(es) in the last {WINDOW_HOURS} h ran without stack skill(s) "
                    f"{', '.join(missing)} their repo calls for — newest tasks {tasks}"
                ),
            ),
        )
    )
