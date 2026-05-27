"""Definition-of-Done gate: a UI-visible ticket needs a local-stack E2E (#88).

The recurrence this forecloses: a ticket whose change is visible in the UI
was repeatedly moved toward "done" without the local-stack end-to-end run,
with the verification deferred to "test on dev after the merge". The deferred
check then surfaced missing scope as unplanned work mid-release. A remembered
rule did not hold; this module is the deterministic substitute.

The check is a pure function over durable state — no network, no clock beyond
what the recipe already stamped:

UI-visible
    A ticket is UI-visible when at least one of its scoped ``repos`` is in the
    active overlay's ``config.frontend_repos``. An overlay with no frontend
    repos configured has no UI-visible tickets, so the gate never fires for a
    backend-only overlay. This reuses the existing overlay signal rather than
    inventing a new label; ``frontend_repos`` is per-overlay configurable.

Satisfying artifact
    A durable ``e2e_recipe.last_run`` with ``result == "green"`` AND
    ``env == "local"`` — a green run of the teatree-managed local stack. A
    ``dev`` run (``env == "dev"``) records provenance but does NOT satisfy the
    gate: the whole point is to catch the gap *before* merge, not after. A run
    with no ``env`` (recorded before #88) is treated conservatively as
    not-local.

Escape hatch
    ``ticket.extra['dod_e2e_override']`` with a non-empty ``reason`` makes the
    gate pass. This is the explicit, audited bypass for a genuinely non-UI or
    exempt ticket the heuristic mis-flags — the gate can never hard-trap a
    legitimate ticket.

The gate is invoked from the ``Ticket.ship()`` transition body — the single
DoD chokepoint every ship path (the loop, ``pr create``, a direct
``ticket transition ship``) funnels through, mirroring the existing
``_refuse_if_worktree_dirty`` preflight. On a block it raises
:class:`DodLocalE2EError`; the transition does not advance.
"""

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

from teatree.core.e2e_workitem import load_recipe
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.overlay_loader import get_overlay

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

LOCAL_ENV = "local"
GREEN_RESULT = "green"


class DodLocalE2EError(InvalidTransitionError):
    """A ship transition was refused: a UI-visible ticket has no local-stack E2E.

    A subclass of :class:`InvalidTransitionError` (sibling of
    ``DirtyWorktreeError``) so the loop's ``Task.complete()`` outer atomic
    rolls the ship advance back and the FSM stays put, exactly like the
    dirty-worktree preflight. The message names the override escape hatch so
    the operator can unblock a legitimately-exempt ticket without code.
    """


def _frontend_repos(ticket: "Ticket") -> list[str]:
    """The active overlay's configured frontend repos, or ``[]`` when unknown.

    Fail-open: an unresolvable overlay (no entry point, ambiguous, mis-named
    ``ticket.overlay``) yields an empty list so the gate degrades to
    "not UI-visible" rather than hard-trapping a ticket on a configuration
    problem unrelated to its work.
    """
    try:
        overlay = get_overlay(ticket.overlay or None)
    except ImproperlyConfigured:
        return []
    return list(overlay.config.frontend_repos)


def is_ui_visible(ticket: "Ticket") -> bool:
    """True iff a scoped repo of *ticket* is in the overlay's frontend repos."""
    frontend = set(_frontend_repos(ticket))
    if not frontend:
        return False
    return any(repo in frontend for repo in (ticket.repos or []))


def has_local_e2e_artifact(ticket: "Ticket") -> bool:
    """True iff the durable recipe records a GREEN run on the LOCAL stack.

    A ``dev`` (deployed) run, a red run, and a run with no recorded env all
    return ``False`` — only a green local-stack run satisfies the DoD.
    """
    last_run = load_recipe(ticket).last_run
    if not last_run:
        return False
    return last_run.get("result") == GREEN_RESULT and last_run.get("env") == LOCAL_ENV


def override_reason(ticket: "Ticket") -> str:
    """The recorded escape-hatch reason, or ``""`` when no override is set."""
    override = (ticket.extra or {}).get("dod_e2e_override") or {}
    return str(override.get("reason", "")).strip()


def check_local_e2e_dod(ticket: "Ticket") -> None:
    """Refuse the ship transition when a UI-visible ticket lacks a local E2E.

    Order of short-circuits (cheapest, most-permissive first):

    1. Not UI-visible → pass (the gate only governs user-visible work).
    2. A recorded override reason → pass (logged for audit).
    3. A green local-stack E2E artifact → pass.
    4. Otherwise → raise :class:`DodLocalE2EError`.
    """
    if not is_ui_visible(ticket):
        return
    reason = override_reason(ticket)
    if reason:
        logger.info(
            "DoD local-E2E gate overridden for ticket %s: %s",
            ticket.pk,
            reason,
        )
        return
    if has_local_e2e_artifact(ticket):
        return
    msg = (
        f"Refusing to ship ticket {ticket} — it is UI-visible (a frontend repo is in "
        f"scope) but has no green local-stack E2E artifact. Run the local-stack E2E "
        f"first (`t3 <overlay> e2e run <work-item>`); a deferred dev-after-merge run "
        f"does not satisfy the Definition of Done. If this ticket is genuinely "
        f"non-UI or exempt, record an override: "
        f"`t3 <overlay> ticket dod-override <id> --reason '<why>'`."
    )
    raise DodLocalE2EError(msg)
