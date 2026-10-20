"""Forced-repro FSM gate on ``ship()`` for FIX-kind tickets (#118).

A FIX ticket cannot ship unless a harness-recorded, provenance-verified
RED->GREEN reproduction exists (or a human-authorized waiver). The gate is a
decision over durable state — no network, no clock, no git: the ancestry
proof was frozen at record time into ``ReproEvidence.provenance_ok``. A disabled
requirement is logged and marked only when it exempts a ticket lacking evidence.

It sits in ``Ticket.ship()`` (not ``mark_delivered()``): the repro record
commands need a LIVE worktree (git for HEAD + ancestry), and ``mark_merged()``
schedules the worktree teardown, so by DELIVERED the worktree is gone and a
forgotten repro can never be recorded. ``ship()`` is the last transition before
teardown — the same reason ``local_e2e_dod`` (which needs the live stack) also
sits in ``ship()``.

Short-circuits satisfied cases before the setting, mirroring
``check_fix_record_dod`` and ``check_e2e_mandatory``:

1. not a FIX ticket -> pass (reuses ``fix_dod_gate.is_fix`` — the #86 SSOT, so
kind classification never diverges).
2. a human-authorized ``ReproWaiver`` -> pass (logged for audit).
3. a provenance-verified RED->GREEN pair -> pass.
4. a disabled requirement -> pass and record the disabling scope.
5. otherwise -> raise :class:`ForcedReproGateError` (naming both remedies).

Because it is an :class:`InvalidTransitionError` subclass, a ``ship()`` that
hits it rolls the advance back and the FSM stays put (identical to
``local_e2e_dod`` / ``fix_record_dod``).
"""

import logging
from typing import TYPE_CHECKING, Literal

from teatree.config import get_effective_settings
from teatree.core.gates.fix_dod_gate import is_fix
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.repro_evidence import ReproEvidence
from teatree.core.models.repro_waiver import ReproWaiver

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


def repro_disabled_scope(ticket: "Ticket") -> str | None:
    try:
        enabled = get_effective_settings(ticket.overlay).forced_repro_gate_enabled
    except ValueError as exc:
        msg = f"Cannot decide forced-repro requirement for ticket {ticket.pk}: {exc}"
        raise ForcedReproGateError(msg) from exc
    if enabled:
        return None
    if ticket.overlay and ConfigSetting.objects.filter(key="forced_repro_gate_enabled", scope=ticket.overlay).exists():
        return ticket.overlay
    return "global"


def forced_repro_gate_enabled(ticket: "Ticket") -> bool:
    return repro_disabled_scope(ticket) is None


def repro_exempt(ticket: "Ticket", *, evidence: Literal["red", "pair"] = "pair") -> bool:
    if not is_fix(ticket):
        return True
    if ReproWaiver.objects.filter(ticket=ticket).exists():
        logger.info("Forced-repro gate waived for ticket %s (human-authorized ReproWaiver)", ticket.pk)
        return True
    if evidence == "red":
        satisfied = ReproEvidence.objects.filter(ticket=ticket).exclude(red_exit_code=0).exists()
    else:
        satisfied = ReproEvidence.objects.has_valid_repro(ticket)
    if satisfied:
        return True
    scope = repro_disabled_scope(ticket)
    if scope is None:
        return False
    logger.warning("forced_repro_gate_enabled is off for ticket %s in scope %s", ticket.pk, scope)
    if scope not in (ticket.extra or {}).get("forced_repro_disabled_scopes", []):
        ticket.merge_extra(append_to_lists={"forced_repro_disabled_scopes": [scope]})
    return True


class ForcedReproGateError(InvalidTransitionError):
    """A FIX-ticket ship was refused: no executed, provenance-verified repro.

    A subclass of :class:`InvalidTransitionError` (sibling of ``DodLocalE2EError``
    / ``FixRecordDodError``) so a ``ship()`` that hits it rolls the advance back
    and the FSM stays put. The message names both satisfiers so the operator can
    unblock without code.
    """


def _deny_message(ticket: "Ticket") -> str:
    return (
        f"Refusing to ship FIX ticket {ticket.pk} — a fix cannot ship without an EXECUTED, provenance-verified "
        f"RED->GREEN reproduction. Without a failing repro captured against the pre-fix tree, a root-cause "
        f"explanation is unreliable. Satisfy the gate with EITHER:\n"
        f"  1. record the executed repro (the harness runs the command and stamps the SHAs):\n"
        f"       t3 <overlay> repro record-red {ticket.pk} --command '<failing-cmd>'   (before the fix)\n"
        f"       t3 <overlay> repro record-green {ticket.pk} --command '<same-cmd>'      (after the fix)\n"
        f"  2. OR a human-authorized waiver for a genuinely repro-less failure (a race/heisenbug):\n"
        f"       t3 <overlay> repro waive {ticket.pk} --approver <user-id> --reason '<why repro-less>'\n"
        f"     the waiver requires the human user — a maker/coding-agent/loop id is refused.\n"
        f"Record a verified reproduction or a human-authorized waiver, then retry."
    )


def check_forced_repro(ticket: "Ticket") -> None:
    """Refuse the ship transition when a FIX ticket lacks an executed repro (#118)."""
    if repro_exempt(ticket):
        return
    raise ForcedReproGateError(_deny_message(ticket))


def check_red_repro(ticket: "Ticket") -> None:
    """Refuse FIX coding completion unless the pre-fix failing run was recorded."""
    if repro_exempt(ticket, evidence="red"):
        return
    msg = (
        f"Refusing to complete coding for FIX ticket {ticket.pk}: record an executed RED reproduction "
        "against the pre-fix HEAD with `repro record-red` before applying the fix."
    )
    raise ForcedReproGateError(msg)


register_gate("forced_repro", check_forced_repro)
register_gate("red_repro", check_red_repro)
