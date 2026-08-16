"""DB-configured autonomous loop (#1796).

A :class:`Loop` row is the durable definition of one autonomous loop: a unique
``name``, exactly one of ``prompt`` (a nullable FK to a reusable
:class:`teatree.core.models.prompt.Prompt`, the instruction to run the loop's
work) or ``script`` (a path to the entry point that runs it), its cadence, an
``enabled`` flag, and ``last_run_at``, the cadence anchor. The loop's logic
stays in its existing Python code; ``prompt``/``script`` only say how to invoke
it, so the row carries config + cadence, not behaviour. ``run_in_sub_agent`` toggles
sub-agent dispatch, ``description`` is human context, and ``overlay`` names the
backend the loop runs against (generically — the stored value is a backend name,
not a hard-coded overlay).

Every loop is autonomous — its own row, its own cadence. There is no single
shared tick (#2650): each enabled loop runs on its own schedule as its own
native Claude ``/loop`` firing ``t3 loops tick --loop <name>``. Cadence is
expressed three ways: ``delay_seconds`` is a fixed interval
between runs (e.g. ``inbox`` every 60s); ``daily_at`` is a once-per-day local
time (e.g. ``news`` at 08:00, ``dream`` at night) that overrides the interval,
making the loop due once per day on or after that wall-clock time; with neither
set (both ``None``) the loop is due every tick.

A never-run loop is due immediately (interval / every-tick) or at its first
scheduled time (daily), so a fresh install fires without waiting a whole window.

``colleague_facing`` (#2904) marks the loops that reach a colleague on the owner's
behalf. It is narrower than the ``colleague`` reach tag each ``MiniLoop`` declares in
code (#3959) and stays a strict subset of it — ``review`` reaches colleagues yet
deliberately keeps running when the owner is away, so self-review does not stall. The
flag is a REPORTING axis (``t3 doctor``'s stale-override finding names the
colleague-facing loops a held mode masks off); which loops actually run is decided
solely by the active :class:`~teatree.core.models.Mode`'s own ``entries`` table.
"""

import datetime as dt
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.loop_state import LoopState

_SECONDS_PER_DAY = 24 * 3600


class LoopManager(models.Manager["Loop"]):
    """Read/transition surface each loop tick uses to drive the autonomous loops."""

    def enabled(self) -> "models.QuerySet[Loop]":
        """The enabled loops — the candidate set the loop-table fan-out considers each pass."""
        return self.filter(enabled=True)

    def mark_run(self, name: str, ts: dt.datetime) -> None:
        """Stamp ``last_run_at = ts`` for *name* — the cadence bump after a run.

        A direct ``update`` so the cadence anchor moves without touching
        ``updated_at`` (which tracks config edits, not runs). ``last_attempt_at``
        moves with it: a run IS an attempt, so no caller can advance the cadence
        anchor while leaving the liveness one behind.
        """
        self.filter(name=name).update(last_run_at=ts, last_attempt_at=ts)

    def mark_attempted(self, name: str, ts: dt.datetime) -> None:
        """Stamp ``last_attempt_at = ts`` — a tick EXECUTED, whatever it produced.

        The half of the anchor a pass may never withhold. ``dream`` withholds
        ``last_run_at`` on a pass that did not stamp success (retry-until-success,
        #2285), which is correct as a cadence decision and a lie to every liveness
        reader: a loop driven every 10 minutes rendered as frozen for 6.6 days
        (#4355). Attempts are stamped BEFORE the pass runs, so a pass killed at its
        deadline still records that it ran.
        """
        self.filter(name=name).update(last_attempt_at=ts)

    def mark_run_if_unchanged(self, name: str, *, previous_last_run_at: dt.datetime | None, now: dt.datetime) -> bool:
        """Atomically claim the cadence anchor: bump ``last_run_at`` iff still ``previous_last_run_at``.

        The lost-update guard against a double-drive (#2777
        follow-up): two ticks that read the SAME ``last_run_at`` would each build
        the loop's jobs and each ``mark_run``, dispatching the loop twice. This is
        the same compare-and-swap shape as
        :meth:`LoopLeaseQuerySet.acquire` — a single conditional ``UPDATE`` whose
        ``WHERE`` matches only when the anchor is still the value the caller read,
        so exactly one of N racing ticks updates 1 row and wins. Django renders
        ``last_run_at=None`` as ``IS NULL``, so the never-run (NULL) anchor is
        handled by the same predicate (``IS NOT DISTINCT FROM``). Returns ``True``
        iff this caller won (updated 1 row).
        """
        won = self.filter(name=name, last_run_at=previous_last_run_at).update(last_run_at=now, last_attempt_at=now)
        return won == 1

    def set_enabled(self, name: str, *, enabled: bool) -> int:
        """Set the ``enabled`` toggle for *name*; return the number of rows updated.

        ``Loop.enabled`` is the row-level source of truth the #2584 loop tick
        reads (``not row.enabled`` skips a loop, independent of the durable
        ``LoopState`` control plane). :meth:`disable` / :meth:`enable` move this
        column in lock-step with their ``LoopState`` write (one atomic method
        owns the paired invariant) so both planes agree. A direct ``update`` is
        idempotent; a name with no row is a no-op (returns ``0``) — the paired
        methods still record their ``LoopState`` intent for a not-yet-seeded name.
        """
        return self.filter(name=name).update(enabled=enabled)

    def disable(self, name: str) -> None:
        """Durably disable *name* on BOTH planes atomically (#1913, #2584).

        The single owner of the paired write the ``loop_state`` command used to
        inline (holistic 3c#4): the ``DISABLED`` :class:`LoopState` kill-switch AND
        the row-level ``enabled=False`` the #2584 tick reads move together in one
        transaction, so no caller can leave one plane stale — the "reports enabled
        but never ticks" bug this method exists to make impossible. A name with no
        ``Loop`` row still records its durable ``LoopState`` intent (the row update
        is a 0-row no-op).
        """
        with transaction.atomic():
            LoopState.objects.disable(name)
            self.set_enabled(name, enabled=False)

    def enable(self, name: str) -> None:
        """Re-enable *name* on BOTH planes atomically — clears EITHER a pause or a disable.

        The inverse of :meth:`disable`: the ``ENABLED`` :class:`LoopState`
        transition (which lifts a PAUSE or a DISABLE) AND ``enabled=True`` move
        together, so both planes agree the loop runs again.
        """
        with transaction.atomic():
            LoopState.objects.enable(name)
            self.set_enabled(name, enabled=True)

    def resume(self, name: str) -> None:
        """Return *name* to running on BOTH planes — the pause-vocabulary alias of :meth:`enable`.

        ``resume`` and ``enable`` are the one "make it run again" transition so a
        loop is never stuck because the operator reached for the pause verb on a
        disabled loop.
        """
        self.enable(name)


class Loop(models.Model):
    """One row per autonomous loop carrying its config and cadence anchor."""

    name = models.CharField(max_length=64, unique=True)
    prompt = models.ForeignKey(
        "core.Prompt",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="loops",
    )
    script = models.CharField(max_length=255, blank=True, default="")
    run_in_sub_agent = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    delay_seconds = models.PositiveIntegerField(null=True, blank=True)
    daily_at = models.TimeField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    colleague_facing = models.BooleanField(default=False)
    last_run_at = models.DateTimeField(null=True, blank=True)
    #: When a tick last EXECUTED, whatever it produced — the anchor no pass may withhold.
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[LoopManager] = LoopManager()

    class Meta:
        db_table = "teatree_loop"
        ordering: ClassVar = ["name"]
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=(models.Q(prompt__isnull=True, script__gt="") | models.Q(prompt__isnull=False, script="")),
                name="loop_prompt_xor_script",
            ),
            models.CheckConstraint(
                condition=models.Q(script="") | models.Q(delay_seconds__isnull=False),
                name="loop_script_requires_delay",
            ),
        ]

    def __str__(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"loop<{self.name} {state} {self.cadence_label}>"

    def clean(self) -> None:
        """Exactly one of ``prompt`` (FK) / ``script``; a script loop carries an interval."""
        if (self.prompt_id is not None) == bool(self.script):  # ty: ignore[unresolved-attribute]
            msg = "Set exactly one of prompt or script."
            raise ValidationError(msg)
        if self.script and self.delay_seconds is None:
            msg = "A script loop requires a delay_seconds interval."
            raise ValidationError(msg)

    @property
    def cadence_label(self) -> str:
        """Human cadence — ``daily 08:00`` scheduled, ``every Ns`` interval, else ``every tick``."""
        if self.daily_at is not None:
            return f"daily {self.daily_at.strftime('%H:%M')}"
        if self.delay_seconds is None:
            return "every tick"
        return f"every {self.delay_seconds}s"

    @property
    def cadence_seconds(self) -> int | None:
        """The declared cadence in seconds — ``None`` for an every-tick loop.

        The same precedence :attr:`cadence_label` renders, because it is the same
        question: every shipped daily row carries a ``delay_seconds`` too (the
        ``loop_script_requires_delay`` constraint), and the slot is what decides when
        it fires. Scaling anything to the stored interval instead would read a daily
        loop's cadence off a column its schedule overrides.
        """
        if self.daily_at is not None:
            return _SECONDS_PER_DAY
        return self.delay_seconds

    def seconds_since_run(self, now: dt.datetime) -> float | None:
        """Seconds since the last run, or ``None`` when the loop never ran."""
        if self.last_run_at is None:
            return None
        return (now - self.last_run_at).total_seconds()

    def is_due(self, now: dt.datetime) -> bool:
        """True when the loop should run under its cadence (interval, daily, or every tick)."""
        if self.daily_at is not None:
            return self._daily_due(now)
        if self.delay_seconds is None:
            return True
        elapsed = self.seconds_since_run(now)
        return elapsed is None or elapsed >= self.delay_seconds

    def next_run_at(self) -> dt.datetime | None:
        """When the loop is next due — interval anchor or the next daily slot.

        ``None`` for an interval loop that has never run (no anchor yet) and for
        a cadence-less loop (no interval, due every tick).
        """
        if self.daily_at is not None:
            return self._next_daily(timezone.now())
        if self.last_run_at is None or self.delay_seconds is None:
            return None
        return self.last_run_at + dt.timedelta(seconds=self.delay_seconds)

    def _daily_due(self, now: dt.datetime) -> bool:
        """Daily-scheduled due gate: due once per day on/after ``daily_at`` local."""
        now_local = self._as_local(now)
        if now_local.time() < self.daily_at:
            return False
        if self.last_run_at is None:
            return True
        return self._as_local(self.last_run_at).date() < now_local.date()

    def _next_daily(self, now: dt.datetime) -> dt.datetime:
        """The next wall-clock occurrence of ``daily_at`` (today if still ahead).

        Each candidate slot is built as ``date + daily_at`` via
        :func:`django.utils.timezone.make_aware`, not by mutating *now*:
        ``now_local.replace(hour=…)`` would carry *now*'s UTC offset (and DST
        ``fold``) onto the target wall-clock even when it lands on the other
        side of a DST transition, and rolling to "tomorrow" with a 24h
        ``timedelta`` would drift by the transition hour. Resolving the offset
        from the active zone at the target instant keeps a non-UTC ``TIME_ZONE``
        correct across spring-forward / fall-back (with ``TIME_ZONE="UTC"``, the
        project default, there is no transition and the result is unchanged).
        """
        now_local = self._as_local(now)
        day = now_local.date() if now_local.time() < self.daily_at else now_local.date() + dt.timedelta(days=1)
        naive_slot = dt.datetime.combine(day, self.daily_at)
        if timezone.is_aware(now_local):
            return timezone.make_aware(naive_slot, timezone.get_current_timezone())
        return naive_slot

    @staticmethod
    def _as_local(when: dt.datetime) -> dt.datetime:
        """Local-zone view of *when* (pass a naive datetime through untouched)."""
        return timezone.localtime(when) if timezone.is_aware(when) else when
