"""Withdraw a pending question whose trigger has healed before anything surfaces it (#4904).

Each :data:`HEAL_CHECKS` entry maps a batch of pending rows to the ones it can PROVE
healed, with a reason. Positive-only: short of proof a row stays pending. The name is
the audit's resolver id, shared with the tick sweep in :mod:`teatree.loop.question_drain`.
"""

from collections.abc import Callable, Iterable, Sequence

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.provision.failure_question import provision_failures_healed

HealCheck = Callable[[Sequence[DeferredQuestion]], dict[int, str]]

HEAL_CHECKS: tuple[tuple[str, HealCheck], ...] = (("provision_healed", provision_failures_healed),)


def withdraw_healed(rows: Iterable[DeferredQuestion]) -> list[DeferredQuestion]:
    """Dismiss every healed row as stale and return the rest, in order."""
    live = list(rows)
    for name, check in HEAL_CHECKS:
        healed = check(live)
        for row in live:
            if (reason := healed.get(row.pk)) is not None:
                row.mark_stale(reason, resolver_id=name)
        live = [row for row in live if row.pk not in healed]
    return live


def live_owner_questions() -> list[DeferredQuestion]:
    """The pending owner-audience backlog, healed rows withdrawn."""
    return withdraw_healed(DeferredQuestion.pending().exclude(audience=DeferredQuestion.Audience.INTERNAL))
