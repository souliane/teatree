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

``colleague_facing`` (#2904) marks a loop that reaches or reads from a
colleague — reviewing someone else's PR, nagging a reviewer, posting where a
teammate reads it — as opposed to internal/self-improvement work. The unified
admission verdict in ``teatree.loops.loop_table`` gates a ``colleague_facing``
row off whenever :func:`teatree.core.availability.resolve_mode` reports
``defers_questions`` (holiday-``away`` or ``autonomous_away``, the same
BLUEPRINT §17.1 invariant 9 axis that defers user-directed questions in that
mode): colleague-facing work should not fire while the user is unreachable to
weigh in, even in ``autonomous_away`` where every other loop keeps
self-pumping.
"""

import datetime as dt
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class LoopManager(models.Manager["Loop"]):
    """Read/transition surface each loop tick uses to drive the autonomous loops."""

    def enabled(self) -> "models.QuerySet[Loop]":
        """The enabled loops — the candidate set the loop-table fan-out considers each pass."""
        return self.filter(enabled=True)

    def due(self, now: dt.datetime) -> "list[Loop]":
        """Enabled loops whose cadence has elapsed (or that never ran)."""
        return [loop for loop in self.enabled() if loop.is_due(now)]

    def mark_run(self, name: str, ts: dt.datetime) -> None:
        """Stamp ``last_run_at = ts`` for *name* — the cadence bump after a run.

        A direct ``update`` so the cadence anchor moves without touching
        ``updated_at`` (which tracks config edits, not runs).
        """
        self.filter(name=name).update(last_run_at=ts)

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
        won = self.filter(name=name, last_run_at=previous_last_run_at).update(last_run_at=now)
        return won == 1

    def set_enabled(self, name: str, *, enabled: bool) -> int:
        """Set the ``enabled`` toggle for *name*; return the number of rows updated.

        ``Loop.enabled`` is the row-level source of truth the #2584 loop tick
        reads (``not row.enabled`` skips a loop, independent of the durable
        ``LoopState`` control plane). The ``enable`` / ``disable`` loop verbs move
        this column in lock-step with their ``LoopState`` write so both planes
        agree. A direct ``update`` is idempotent; a name with no row is a no-op
        (returns ``0``) — the loop-config verbs still record their ``LoopState``
        intent for a not-yet-seeded name.
        """
        return self.filter(name=name).update(enabled=enabled)


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
        """The next wall-clock occurrence of ``daily_at`` (today if still ahead)."""
        now_local = self._as_local(now)
        today_at = now_local.replace(hour=self.daily_at.hour, minute=self.daily_at.minute, second=0, microsecond=0)
        if now_local.time() < self.daily_at:
            return today_at
        return today_at + dt.timedelta(days=1)

    @staticmethod
    def _as_local(when: dt.datetime) -> dt.datetime:
        """Local-zone view of *when* (pass a naive datetime through untouched)."""
        return timezone.localtime(when) if timezone.is_aware(when) else when
