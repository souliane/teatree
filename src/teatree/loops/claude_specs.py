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

**Verify-by-reread (#1192).** A CLI cannot call ``CronCreate`` itself, so it
cannot confirm the registration landed either — the agent's own harness call
is the only truth. :func:`spec_registered` and :func:`verify_loop_registered`
close that gap on the READ side: given a ``CronList`` snapshot the agent
fetches after calling ``CronCreate``, they confirm the loop's registration is
actually present, using the same backtick-terminated-token match the
enable/disable skill already uses. ``t3 loop verify-cron <name>`` is the CLI
surface (:mod:`teatree.cli.loop_verify_cron`).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.verify_by_reread import RereadOutcome, verify_by_reread

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
    enabled-and-un-held set the loop-table fan-out actually dispatches (#2584). Keying on
    ``Loop.enabled`` alone (the prior bug) left a paused loop firing every cadence.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415
    from teatree.core.models import Loop  # noqa: PLC0415
    from teatree.loop.loop_state_db import loop_enabled  # noqa: PLC0415

    # #1796 — when the singleton `t3 worker` owns the cadence (draining the
    # self-rescheduling loop-timer chains), the native `/loop` cron driver stands
    # down entirely: zero rows are mirrored, so the owner-session bootstrap emits NO
    # `CronCreate` and the two drivers never both fire. Default-OFF: the native crons
    # drive as today until the operator opts into the worker. (The reactive-slot
    # `/loop <duration>` registrations are a separate seam and are not CronCreate, so
    # they are unaffected here.)
    if get_effective_settings().loop_runner_enabled:
        return []
    return [claude_loop_spec(loop) for loop in Loop.objects.enabled() if loop_enabled(loop.name)]


_LOOP_TOKEN_RE = re.compile(r"`t3 loops tick --loop [^\s`]+`")


def _loop_token(prompt: str) -> str | None:
    """The backtick-terminated ``t3 loops tick --loop <name>`` token embedded in *prompt*."""
    match = _LOOP_TOKEN_RE.search(prompt)
    return match.group(0) if match else None


def _cron_matches(job: Mapping[str, object], expected_cron: str) -> bool:
    """True unless *job* names a schedule that disagrees with *expected_cron*.

    A stale native job can keep a matching ``prompt`` after the loop's
    cadence changed in the DB — prompt-token matching alone would then
    report ``confirmed`` for a job firing on the WRONG schedule, defeating
    the point of verification (codex review, #1192). ``CronCreate`` takes
    its schedule as ``cron=...``, so a ``CronList`` snapshot is expected to
    echo it back under the same key; a job that omits the key degrades to
    "schedule unknown, don't contradict the prompt match" rather than a
    false negative against a harness snapshot shape we haven't confirmed.
    """
    cron = job.get("cron")
    if not isinstance(cron, str) or not cron:
        return True
    return cron == expected_cron


def spec_registered(spec: ClaudeLoopSpec, jobs: Iterable[Mapping[str, object]]) -> bool:
    """True when *jobs* (a harness ``CronList`` snapshot) contains *spec*'s registration.

    Matches by the same backtick-terminated ``t3 loops tick --loop <name>``
    token the enable/disable skill uses to disambiguate one loop's cron from
    another (a bare ``--loop ship`` substring would also match
    ``--loop ship-fast``), AND — when the job names a schedule — that it
    agrees with *spec*'s expected cron (:func:`_cron_matches`). A non-dict
    job, or one whose ``prompt`` field is not a string, is skipped rather
    than raising — a harness snapshot with unexpected shape degrades to
    "not found", never a crash.
    """
    token = _loop_token(spec.prompt)
    if token is None:
        return False
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        prompt = job.get("prompt")
        if isinstance(prompt, str) and token in prompt and _cron_matches(job, spec.cron):
            return True
    return False


def verify_loop_registered(spec: ClaudeLoopSpec, jobs: Iterable[Mapping[str, object]]) -> RereadOutcome:
    """Verify-by-reread (#1192): confirm *spec*'s ``CronCreate`` registration is visible.

    ``jobs`` is the harness ``CronList`` snapshot the agent fetches right
    after calling ``CronCreate`` — this never calls the harness itself (a CLI
    cannot), it only judges a snapshot the caller already has in hand.
    """
    jobs_list = list(jobs)
    return verify_by_reread(
        label=f"cron_registration:{spec.slot_id}",
        reread=lambda: spec_registered(spec, jobs_list),
    )
