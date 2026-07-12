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

from teatree.core.intake.e2e_workitem import load_recipe
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.overlay_loader import frontend_repos_for_overlay

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import DodE2EViolation

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


class _UiVisibilityUndeterminedError(Exception):
    """The overlay's frontend-repo config could not be resolved (#1426).

    Raised internally only when ``ticket.overlay`` resolves to no registered
    overlay at all — a removed overlay, a typo, a synthetic tag. The gate
    cannot tell whether such a ticket is UI-visible, so it must NOT silently
    treat it as backend-only. A safety gate fails CLOSED: ``is_ui_visible``
    maps this to "presumed UI-visible" so the DoD check still runs, and the
    override / artifact escape hatches in ``check_local_e2e_dod`` keep it
    from ever locking out.

    A **path-only** TOML overlay (a ``path`` but no Python ``class``, reached
    through the CLI subprocess bridge) is NOT undetermined: it is a known,
    registered overlay whose ``frontend_repos`` are read from its
    ``[overlays.<name>]`` config table, so an in-process gate on its tickets
    no longer fails closed for every ticket.
    """


def _frontend_repos(ticket: "Ticket") -> list[str]:
    """The active overlay's configured frontend repos.

    Resolves through :func:`frontend_repos_for_overlay`, which answers for
    path-only TOML overlays (reached via the CLI subprocess bridge, so not
    instantiable as :class:`OverlayBase` in this process) from their config
    table. Raises :class:`_UiVisibilityUndeterminedError` only when the
    overlay resolves to nothing registered — the genuinely-undetermined case
    the caller must fail closed on rather than infer "not UI-visible" from
    the absence of an answer.
    """
    try:
        return frontend_repos_for_overlay(ticket.overlay or None)
    except ImproperlyConfigured as exc:
        raise _UiVisibilityUndeterminedError(str(exc)) from exc


def is_ui_visible(ticket: "Ticket") -> bool:
    """True iff a scoped repo of *ticket* is in the overlay's frontend repos.

    A ticket with **no scoped repos** is never UI-visible — there is nothing
    that could intersect ``frontend_repos`` — so the answer is deterministic
    regardless of overlay resolvability; the fail-closed branch is reserved
    for the genuinely-ambiguous case (repos exist but cannot be classified).

    Fails CLOSED on an undeterminable overlay config (#1426): when the ticket
    HAS scoped repos but the gate cannot resolve the overlay's
    ``frontend_repos``, it presumes the ticket UI-visible (returns ``True``)
    and logs loudly, so a misconfigured instance cannot silently SKIP the
    safety gate. The override and green-artifact escape hatches downstream
    keep this fail-closed posture from becoming a hard lockout.
    """
    repos = ticket.repos or []
    if not repos:
        return False
    try:
        frontend = set(_frontend_repos(ticket))
    except _UiVisibilityUndeterminedError as exc:
        logger.warning(
            "DoD local-E2E gate: cannot resolve UI-visibility for ticket %s (%s); "
            "failing CLOSED — presuming UI-visible so the gate is not silently skipped. "
            "Record an override or a green local E2E to proceed.",
            ticket.pk,
            exc,
        )
        return True
    if not frontend:
        return False
    return any(repo in frontend for repo in repos)


def has_local_e2e_artifact(ticket: "Ticket") -> bool:
    """True iff the durable recipe records a GREEN run on the LOCAL stack.

    A ``dev`` (deployed) run, a red run, and a run with no recorded env all
    return ``False`` — only a green local-stack run satisfies the DoD.

    A malformed ``last_run`` (corrupt durable JSON that deserialised to a
    non-mapping — a string, list, …) is treated as "no valid artifact": the
    DoD is conservatively unmet, not an unhandled raise. The gate would
    rather refuse the ship than crash on garbage state.
    """
    last_run = load_recipe(ticket).last_run
    if not isinstance(last_run, dict):
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


_DOD_VIOLATION_KEY = "dod_e2e_violation"


def _state_index(state: str) -> int:
    from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return [s.value for s in Ticket.State].index(state)


def _is_post_ship_state(state: str) -> bool:
    """True iff *state* is at or past SHIPPED on the lifecycle.

    Every such state is downstream of a successful ship: SHIPPED, the higher
    IN_REVIEW (reached via ``ship() → request_review()``), and the terminal
    MERGED / DELIVERED. A sync writer that grants any of these on a
    UI-visible ticket with no local E2E bypasses the ``ship()`` DoD gate.
    """
    from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return _state_index(state) >= _state_index(Ticket.State.SHIPPED)


def sync_gate_allows(ticket: "Ticket", inferred_state: str) -> bool:
    """True iff automated sync may grant *inferred_state* on *ticket*.

    The single DoD decision every sync writer shares (#1426): a pre-ship
    state is always allowed; a post-ship state (>= SHIPPED) is allowed only
    when the gate's own decision (:func:`check_local_e2e_dod`) passes — the
    same UI-visible / override / green-artifact policy the ``ship()``
    transition enforces. This is what stops automated PR sync from advancing
    a UI-visible no-E2E ticket past ship outside the FSM.
    """
    if not _is_post_ship_state(inferred_state):
        return True
    try:
        check_local_e2e_dod(ticket)
    except DodLocalE2EError:
        return False
    return True


def workflow_capped_state(ticket: "Ticket", inferred_state: str) -> str:
    """Cap a NON-terminal sync-inferred state at the DoD gate, demoting to STARTED.

    For workflow states inferred from a live, still-open PR (SHIPPED:
    non-draft no-approvals, IN_REVIEW: non-draft with approvals / a requested
    reviewer). When the gate refuses, the sync demotes to STARTED — "an open
    non-draft PR exists, but the DoD is not yet met" — and leaves the ship
    transition for the ``ship()`` path to own once the local E2E lands.
    Pre-ship states and a gate-allowed post-ship state pass through unchanged,
    so normal pre-ship PR-state syncing is preserved.

    Terminal states that reflect an external fact (a genuinely merged or
    deployed PR) must NOT be routed here — demoting them would make the
    ticket contradict reality. Use :func:`record_terminal_dod_violation`
    for those.
    """
    from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if sync_gate_allows(ticket, inferred_state):
        return inferred_state
    logger.info(
        "PR sync withheld post-ship state %s for ticket %s: DoD local-E2E gate not "
        "satisfied; leaving the ship transition to own it.",
        inferred_state,
        ticket.pk,
    )
    return Ticket.State.STARTED


def record_terminal_dod_violation(ticket: "Ticket", terminal_state: str) -> None:
    """Surface a DoD violation on a TERMINAL state that reflects external reality.

    A genuinely merged/deployed PR is a fact the sync must follow — demoting
    the ticket to STARTED would make it lie about reality (its own bug, and
    inconsistent with how the ``reconcile_merged`` FSM keystone follows an
    authorised post-hoc merge). The gate's purpose is to stop ADVANCING to a
    post-ship state without a local E2E, not to rewrite terminal reality.

    So for MERGED / DELIVERED the writer keeps the terminal state but, when
    the DoD was not met, records a durable ``dod_e2e_violation`` marker and
    logs loudly so the gap is auditable rather than silent. A no-op when the
    gate would have allowed the state (UI-visible with a green local E2E /
    override, or not UI-visible).
    """
    if sync_gate_allows(ticket, terminal_state):
        return
    logger.warning(
        "DoD local-E2E gate VIOLATED on terminal state %s for ticket %s: a UI-visible "
        "ticket reached a terminal state with no green local-stack E2E artifact. The "
        "terminal state reflects external reality and is kept; recording a durable "
        "dod_e2e_violation marker for audit.",
        terminal_state,
        ticket.pk,
    )
    existing = (ticket.extra or {}).get(_DOD_VIOLATION_KEY)
    if isinstance(existing, dict) and existing.get("state") == terminal_state:
        return
    marker: DodE2EViolation = {
        "state": terminal_state,
        "at": _now_iso(),
        "detail": "terminal state synced without a green local-stack E2E artifact",
    }
    ticket.merge_extra(set_keys={_DOD_VIOLATION_KEY: marker})


def _now_iso() -> str:
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return timezone.now().isoformat()


register_gate("local_e2e_dod", check_local_e2e_dod)
