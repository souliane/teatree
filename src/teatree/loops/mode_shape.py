"""The structural invariants a mode's loop mask must satisfy, whoever wrote it (#4096, #4188).

**A mode that stops the BOX surviving must not leave it consuming.** ``off`` masked every
survival loop off — ``resource_pressure`` and ``idle_stack_reaper``, the two that recovered
this box from both of one day's out-of-memory emergencies — while ``db_backup`` kept
writing. That combination can only ever consume disk, and it is reached exactly when an
operator grabs a "stop everything" mode mid-incident, with no recovery path left inside the
box. So there is a LOAD-BEARING tier (:data:`LOAD_BEARING_LOOPS`) no mask may quiet, the
low-power/token-budget mode excepted, and no mask may admit the backup while the whole
reclaim pair is quiet.

**A mode that stops the pipeline DRAINING must not leave it FILLING.** ``maintenance``
masked ``ship`` and ``tickets`` off for the overnight window but named no opinion on
``issue_implementer``, which therefore inherited ``Loop.enabled`` and kept claiming issues
— 13h a night of work the delivery lane could not merge. Nothing in
:mod:`teatree.loops.seed_inertness` could see it: every row was present and every loop was
firing, which is exactly what that audit asks.

The rule is a property of the MASK rather than of one named mode, so it holds for an
operator-written mode as much as a shipped one. The check takes no ORM — the same predicate
judges a live :class:`teatree.core.models.Mode` row and a shipped
:class:`teatree.loops.preset_seed.PresetSpec` — but it is NOT pure over the mask alone:
*absent* means inherit, so what an absent intake entry resolves to depends on that loop's
own ``Loop.enabled``. The base flags are therefore an input. Assuming the worst instead
would fire on every mask that legitimately masks delivery while intake is parked, and a
report that is noisy out of the box is one people learn to ignore.
"""

from collections.abc import Mapping
from dataclasses import dataclass

#: Loops that DRAIN the pipeline — masking one off stops work leaving the factory.
DELIVERY_LOOPS: tuple[str, ...] = ("ship", "tickets")

#: Loops that FILL it — admitting one while delivery is masked is the stalling asymmetry.
INTAKE_LOOPS: tuple[str, ...] = ("issue_implementer",)

#: Loops that keep the BOX alive and reachable rather than the factory productive: the
#: reclaim pair, the two janitors that keep provisioning from starving, and ``inbox``, the
#: only channel an operator can reach the box through. A mask may not quiet one — a mode is
#: most likely to be switched during the very incident that needs them.
LOAD_BEARING_LOOPS: tuple[str, ...] = (
    "housekeeping",
    "idle_stack_reaper",
    "inbox",
    "local_stack_queue",
    "resource_pressure",
)

#: The load-bearing subset that RECLAIMS disk — with every one quiet nothing frees space.
DISK_RECLAIM_LOOPS: tuple[str, ...] = ("idle_stack_reaper", "resource_pressure")

#: The loop that CONSUMES disk on a cadence: a backup pass writes, it never frees.
BACKUP_LOOP = "db_backup"

INHERITS_ON = "no entry, and its Loop row is enabled, so it inherits ON"
FORCED_ON = "forced on by the mask"
INHERITS_OFF = "no entry, and its Loop row is disabled, so it inherits OFF"
MASKED_OFF = "masked off"


@dataclass(frozen=True, slots=True)
class IntakeWithoutDelivery:
    """A mask that stops the pipeline draining while leaving it filling."""

    masked_delivery: tuple[str, ...]
    #: Each admitted intake loop with HOW it stays admitted — the part an operator must act on.
    admitted_intake: tuple[tuple[str, str], ...]

    @property
    def detail(self) -> str:
        admitted = ", ".join(f"{loop} ({why})" for loop, why in self.admitted_intake)
        remedy = " ".join(f"`t3 loop preset edit <mode> --set {loop}=off`" for loop, _ in self.admitted_intake)
        return (
            f"masks delivery off ({', '.join(self.masked_delivery)}) but still admits {admitted} — "
            f"the factory keeps claiming and implementing issues that nothing can merge. "
            f"Mask the intake loop off in this mode too ({remedy}), or unmask delivery."
        )


def intake_without_delivery(
    entries: Mapping[str, object], *, base_enabled: Mapping[str, bool]
) -> IntakeWithoutDelivery | None:
    """The asymmetry in *entries*, or ``None`` when the mask drains at least as fast as it fills.

    *base_enabled* carries each intake loop's own ``Loop.enabled``, which is what an absent
    entry inherits. A loop it does not name reads as not running — the fail-quiet direction,
    since a loop with no row cannot claim anything.
    """
    masked = tuple(loop for loop in DELIVERY_LOOPS if loop_opinion(entries, loop) is False)
    if not masked:
        return None
    admitted = tuple(
        (loop, FORCED_ON if opinion else INHERITS_ON)
        for loop in INTAKE_LOOPS
        if (opinion := loop_opinion(entries, loop)) or (opinion is None and base_enabled.get(loop, False))
    )
    return IntakeWithoutDelivery(masked_delivery=masked, admitted_intake=admitted) if admitted else None


@dataclass(frozen=True, slots=True)
class BackupWithoutReclaim:
    """A mask that keeps writing backups with nothing left that can free the space."""

    #: HOW the backup stays admitted — the half an operator must act on.
    admitted_backup: str
    quieted_reclaim: tuple[tuple[str, str], ...]

    @property
    def detail(self) -> str:
        quieted = ", ".join(f"{loop} ({why})" for loop, why in self.quieted_reclaim)
        remedy = " ".join(f"`t3 loop preset edit <mode> --set {loop}=on`" for loop, _ in self.quieted_reclaim)
        return (
            f"keeps {BACKUP_LOOP} admitted ({self.admitted_backup}) while every reclaim loop is quiet "
            f"({quieted}) — the box goes on writing backups with nothing that can free the space, so "
            f"this mask can only ever consume disk. Admit the reclaim loops ({remedy}), or mask "
            f"{BACKUP_LOOP} off too."
        )


def backup_without_reclaim(
    entries: Mapping[str, object], *, base_enabled: Mapping[str, bool]
) -> BackupWithoutReclaim | None:
    """The consume-without-relief shape in *entries*, or ``None`` when something can still free disk.

    *base_enabled* carries each loop's own ``Loop.enabled``, which is what an absent entry
    inherits. The two unknown-base directions are deliberately opposite, and both are the
    conservative one for their side: an unnamed backup reads as not writing (no finding out
    of a base the caller could not answer for), while an unnamed reclaim loop reads as quiet
    — a loop nobody can vouch for is not evidence that the box can still be relieved.
    """
    backup = loop_opinion(entries, BACKUP_LOOP)
    if not (backup or (backup is None and base_enabled.get(BACKUP_LOOP, False))):
        return None
    quieted = tuple(
        (loop, MASKED_OFF if opinion is False else INHERITS_OFF)
        for loop in DISK_RECLAIM_LOOPS
        if (opinion := loop_opinion(entries, loop)) is False or (opinion is None and not base_enabled.get(loop, False))
    )
    if len(quieted) < len(DISK_RECLAIM_LOOPS):
        return None
    return BackupWithoutReclaim(admitted_backup=FORCED_ON if backup else INHERITS_ON, quieted_reclaim=quieted)


def quieted_load_bearing(entries: Mapping[str, object]) -> tuple[str, ...]:
    """The load-bearing loops *entries* forces OFF, in declaration order.

    Only an explicit ``False`` counts: an absent entry hands the decision to the loop's own
    column, which is the base-config plane rather than something the mask did.
    """
    return tuple(loop for loop in LOAD_BEARING_LOOPS if loop_opinion(entries, loop) is False)


def loop_opinion(entries: Mapping[str, object], loop: str) -> bool | None:
    """The tri-state opinion, degrading a non-bool to inherit exactly as ``Mode.state_for`` does."""
    value = entries.get(loop)
    return value if isinstance(value, bool) else None
