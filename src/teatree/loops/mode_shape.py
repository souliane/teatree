"""The structural invariant a mode's loop mask must satisfy, whoever wrote it (#4096).

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

INHERITS_ON = "no entry, and its Loop row is enabled, so it inherits ON"
FORCED_ON = "forced on by the mask"


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


def loop_opinion(entries: Mapping[str, object], loop: str) -> bool | None:
    """The tri-state opinion, degrading a non-bool to inherit exactly as ``Mode.state_for`` does."""
    value = entries.get(loop)
    return value if isinstance(value, bool) else None
