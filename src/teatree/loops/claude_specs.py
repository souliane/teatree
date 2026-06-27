"""Map enabled DB ``Loop`` rows → native Claude Code ``/loop`` specs (#2650).

The single source of truth the owner-session hook AND the ``/t3:loops``
enable/disable skill both read. The DB ``Loop`` table is canonical; the live set
of native Claude ``/loop``s MIRRORS the set of ENABLED rows — ONE ``/loop`` per
enabled row (PER-LOOP, not per-group). There is no single fat tick: each enabled
loop is its own native ``/loop`` firing on that loop's own cadence.

Each :class:`ClaudeLoopSpec` carries the values a harness ``CronCreate`` needs to
register one loop (``CronCreate`` takes no id and returns its OWN harness job id —
there is nothing to pass in):

- ``slot_id`` — a STABLE LABEL per loop name (``t3-loop-<name>``): a display id to
    recognise a loop's cron. It is NOT the delete key — the harness assigns the
    job id at ``CronCreate`` time;
- ``cron`` — derived from the row's own cadence (``daily_at`` wall-clock,
    ``delay_seconds`` interval, or every-minute when the row is cadence-less);
- ``prompt`` — the recurring prompt the ``/loop`` submits each fire: run THAT one
    loop via ``t3 loops tick --loop <name>`` (the DB-master scoped to a single
    row, claiming the per-loop ``loop:<name>`` lease), then report the summary.

The delete-time disambiguator is the BACKTICK-TERMINATED ```` `t3 loops tick
--loop <name>` ```` token in the ``prompt`` (the closing backtick is load-bearing:
a bare ``--loop ship`` substring would also match ``--loop ship-fast``). To
disable a loop the skill ``CronList``s, matches the job whose prompt equals this
spec's ``prompt`` — equivalently, contains that exact backtick-terminated token —
and ``CronDelete``s it by the harness job id.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models import Loop

_SLOT_ID_PREFIX = "t3-loop-"

#: The recurring ``/loop`` prompt template — runs ONE enabled loop per fire. Kept
#: in sync with the hook-side recogniser ``hooks/scripts/loop_registrations.py``
#: (a parity test pins the two together).
_RUN_PROMPT_TEMPLATE = "Run `t3 loops tick --loop {name}` in Bash, then briefly report the tick summary."

_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24


@dataclass(frozen=True, slots=True)
class ClaudeLoopSpec:
    """One enabled loop's native Claude ``/loop`` registration spec."""

    slot_id: str
    cron: str
    prompt: str


def loop_slot_id(name: str) -> str:
    """The STABLE per-loop LABEL ``t3-loop-<name>`` (a display id, NOT the harness delete key)."""
    return f"{_SLOT_ID_PREFIX}{name}"


def loop_run_prompt(name: str) -> str:
    """The recurring prompt a loop's ``/loop`` fires — run only that loop by name."""
    return _RUN_PROMPT_TEMPLATE.format(name=name)


def cron_for_loop(loop: "Loop") -> str:
    """The cron expression for a loop's cadence — wall-clock daily, interval, or every minute.

    A ``daily_at`` row fires once per day at that wall-clock minute/hour. An
    interval row (``delay_seconds``) fires every ``N`` minutes (sub-hour), at the
    top of every ``H`` hours (hour-aligned), or once at midnight (a day or
    longer). A cadence-less row (both unset — due every tick) fires every minute.
    """
    if loop.daily_at is not None:
        return f"{loop.daily_at.minute} {loop.daily_at.hour} * * *"
    if loop.delay_seconds is None:
        return "* * * * *"
    return _cron_from_seconds(loop.delay_seconds)


def _cron_from_seconds(seconds: int) -> str:
    """Convert an interval in seconds to the coarsest valid cron expression."""
    minutes = max(1, seconds // 60)
    if minutes < _MINUTES_PER_HOUR:
        return f"*/{minutes} * * * *"
    hours = minutes // _MINUTES_PER_HOUR
    if hours >= _HOURS_PER_DAY:
        return "0 0 * * *"
    if hours == 1:
        return "0 * * * *"
    return f"0 */{hours} * * *"


def claude_loop_spec(loop: "Loop") -> ClaudeLoopSpec:
    """The native Claude ``/loop`` spec for one (enabled) loop row."""
    return ClaudeLoopSpec(
        slot_id=loop_slot_id(loop.name),
        cron=cron_for_loop(loop),
        prompt=loop_run_prompt(loop.name),
    )


def enabled_loop_specs() -> list[ClaudeLoopSpec]:
    """One spec per loop the single enable verdict admits, name-ordered — the live ``/loop`` set to mirror.

    Routes through :func:`teatree.loop.loop_state_db.loop_enabled` (``Loop.enabled``
    AND not ``LoopState``-held) so a ``t3 loop pause`` / ``disable`` loop is NOT
    mirrored as a firing no-op cron — the registered cron set never drifts from the
    enabled-and-un-held set the master actually dispatches (#2584). Keying on
    ``Loop.enabled`` alone (the prior bug) left a paused loop firing every cadence.
    """
    from teatree.core.models import Loop  # noqa: PLC0415
    from teatree.loop.loop_state_db import loop_enabled  # noqa: PLC0415

    return [claude_loop_spec(loop) for loop in Loop.objects.enabled() if loop_enabled(loop.name)]
