"""Forced-repro FSM gate on ``ship()`` for FIX-kind tickets (#118).

A FIX ticket cannot ship unless a harness-recorded, provenance-verified
RED->GREEN reproduction exists (or a human-authorized waiver). The gate is a
pure decision over durable state — no network, no clock, no git: the ancestry
proof was frozen at record time into ``ReproEvidence.provenance_ok``, so the gate
is a single DB read.

It sits in ``Ticket.ship()`` (not ``mark_delivered()``): the repro record
commands need a LIVE worktree (git for HEAD + ancestry), and ``mark_merged()``
schedules the worktree teardown, so by DELIVERED the worktree is gone and a
forgotten repro can never be recorded. ``ship()`` is the last transition before
teardown — the same reason ``local_e2e_dod`` (which needs the live stack) also
sits in ``ship()``.

Short-circuits cheapest / most-permissive first, mirroring
``check_fix_record_dod`` and ``check_e2e_mandatory``:

1. ``require_executed_repro`` OFF for the overlay -> pass (DARK default: a total
no-op at ``ship()`` for every ticket until an overlay opts in).
2. not a FIX ticket -> pass (reuses ``fix_dod_gate.is_fix`` — the #86 SSOT, so
kind classification never diverges).
3. a human-authorized ``ReproWaiver`` -> pass (logged for audit).
4. a provenance-verified RED->GREEN pair -> pass.
5. otherwise -> raise :class:`ForcedReproGateError` (naming both remedies).

Because it is an :class:`InvalidTransitionError` subclass, a ``ship()`` that
hits it rolls the advance back and the FSM stays put (identical to
``local_e2e_dod`` / ``fix_record_dod``).
"""

import logging
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.gates.fix_dod_gate import is_fix
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.repro_evidence import ReproEvidence
from teatree.core.models.repro_waiver import ReproWaiver

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


class ForcedReproGateError(InvalidTransitionError):
    """A FIX-ticket ship was refused: no executed, provenance-verified repro.

    A subclass of :class:`InvalidTransitionError` (sibling of ``DodLocalE2EError``
    / ``FixRecordDodError``) so a ``ship()`` that hits it rolls the advance back
    and the FSM stays put. The message names both satisfiers so the operator can
    unblock without code.
    """


def _require_executed_repro(overlay_name: str | None) -> bool:
    return bool(get_effective_settings(overlay_name).require_executed_repro)


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
        f"The operator can disable the gate entirely: "
        f"t3 <overlay> config_setting set require_executed_repro false --overlay <name>."
    )


def check_forced_repro(ticket: "Ticket") -> None:
    """Refuse the ship transition when a FIX ticket lacks an executed repro (#118)."""
    if not _require_executed_repro(ticket.overlay or None):
        return
    if not is_fix(ticket):
        return
    if ReproWaiver.objects.filter(ticket=ticket).exists():
        logger.info("Forced-repro gate waived for ticket %s (human-authorized ReproWaiver)", ticket.pk)
        return
    if ReproEvidence.objects.has_valid_repro(ticket):
        return
    raise ForcedReproGateError(_deny_message(ticket))


register_gate("forced_repro", check_forced_repro)
