"""The IMPLEMENT phase — synthetic mechanism ticket + normal maker pipeline (north-star PR-7).

Cloned from :func:`teatree.loops.outer_loop.implement.schedule_experiment_fix`: an
ADMITTED directive whose sketch is a ``setting_policy_gate`` anchors an ``AUTHOR``
``Ticket`` on a unique synthetic issue URL and rides :meth:`Ticket.schedule_coding`
— the SAME isolated-worktree → plan gate → design/quality critic → cold review →
critic-gated merge keystone every ticket uses. The self-modification code is held
to the clean bar by those gates for free; the directive loop gains ZERO new merge
authority. ``extra["directive_id"]`` links the ticket back so the directive-scoped
plan gates (PR-3's ``mechanism_placement``) key on it.

The admission baseline is snapshotted here (mirroring the outer loop's propose-time
baseline) so VERIFYING's no-collateral-regression evidence has a reference.
"""

from teatree.core.models import Directive, FactoryScoreSnapshot
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loops.outer_loop.score import read_score

#: The standing north-star self-modification umbrella every directive's synthetic
#: mechanism ticket anchors under; the ``#directive-impl=<pk>`` fragment makes each
#: unique while still resolving the ``souliane/teatree`` overlay via
#: ``infer_overlay_for_url`` (the outer-loop synthetic-ticket idiom).
DIRECTIVE_IMPL_UMBRELLA_URL = "https://github.com/souliane/teatree/issues/3009"

_DIRECTIVE_KEY = "directive_id"


def _baseline_snapshot(directive: Directive) -> FactoryScoreSnapshot:
    """Record the admission-baseline factory score for the directive's scope."""
    overlay = directive.scope_overlay
    return FactoryScoreSnapshot.objects.record_snapshot(read_score(overlay=overlay), overlay=overlay)


def schedule_directive_implementation(
    directive: Directive,
    *,
    umbrella_url: str = DIRECTIVE_IMPL_UMBRELLA_URL,
) -> Task | None:
    """Anchor the directive's synthetic mechanism ticket + schedule its coding task.

    Idempotent per directive (the synthetic issue URL dedups); snapshots the
    admission baseline and transitions the directive ``ADMITTED`` → ``IMPLEMENTING``.
    Returns the scheduled ``Task`` (``None`` when a coding task already exists).
    """
    issue_url = f"{umbrella_url}#directive-impl={directive.pk}"
    short = (directive.constraint_statement or directive.raw_text).strip()[:80]
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"role": Ticket.Role.AUTHOR, "short_description": short},
    )
    extra = dict(ticket.extra or {})
    extra[_DIRECTIVE_KEY] = directive.pk
    if extra != ticket.extra:
        ticket.extra = extra
        ticket.save(update_fields=["extra"])
    task: Task | None = None
    already_scheduled = Task.objects.pending_in_phase("coding").filter(ticket=ticket).exists()
    if not already_scheduled and ticket.state == Ticket.State.NOT_STARTED:
        task = ticket.schedule_coding()
    directive.begin_implementation(ticket, baseline_snapshot=_baseline_snapshot(directive))
    return task


def skip_directive_implementation(directive: Directive) -> None:
    """The ``activation_only`` path: snapshot the baseline + skip straight to CONFIGURING.

    A directive whose interpreter found the generic mechanism already exists has no
    code to build, so there is no synthetic ticket — only the admission baseline and
    the ``ADMITTED`` → ``CONFIGURING`` transition.
    """
    directive.skip_to_configuring(baseline_snapshot=_baseline_snapshot(directive))
