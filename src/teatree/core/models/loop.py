"""DB-configured autonomous loop (#1796).

A :class:`Loop` row is the durable definition of one autonomous loop: a unique
``name``, exactly one of ``prompt`` (an instruction to run the loop's work) or
``script`` (a path to the entry point that runs it), its cadence, an ``enabled``
flag, and ``last_run_at``, the cadence anchor. The loop's logic stays in its
existing Python code; ``prompt``/``script`` only say how to invoke it, so the
row carries config + cadence, not behaviour. ``run_in_sub_agent`` toggles
sub-agent dispatch, ``description`` is human context, and ``overlay`` names the
backend the loop runs against (generically — the stored value is a backend name,
not a hard-coded overlay).

Every loop is autonomous — its own row, its own cadence. There is no single fat
main tick: the master session (#1796) runs every enabled loop on its own
schedule. Cadence is expressed three ways: ``delay_seconds`` is a fixed interval
between runs (e.g. ``inbox`` every 60s); ``daily_at`` is a once-per-day local
time (e.g. ``news`` at 08:00, ``dream`` at night) that overrides the interval,
making the loop due once per day on or after that wall-clock time; with neither
set (both ``None``) the loop is due every tick.

A never-run loop is due immediately (interval / every-tick) or at its first
scheduled time (daily), so a fresh install fires without waiting a whole window.
"""

import datetime as dt
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class LoopManager(models.Manager["Loop"]):
    """Read/transition surface the master uses to drive the autonomous loops."""

    def enabled(self) -> "models.QuerySet[Loop]":
        """The enabled loops — the candidate set the master considers each pass."""
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


class Loop(models.Model):
    """One row per autonomous loop carrying its config and cadence anchor."""

    name = models.CharField(max_length=64, unique=True)
    prompt = models.TextField(blank=True, default="")
    script = models.CharField(max_length=255, blank=True, default="")
    run_in_sub_agent = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    delay_seconds = models.PositiveIntegerField(null=True, blank=True)
    daily_at = models.TimeField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[LoopManager] = LoopManager()

    class Meta:
        db_table = "teatree_loop"
        ordering: ClassVar = ["name"]
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(prompt="", script__gt="") | models.Q(prompt__gt="", script=""),
                name="loop_prompt_xor_script",
            ),
        ]

    def __str__(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"loop<{self.name} {state} {self.cadence_label}>"

    def clean(self) -> None:
        """Exactly one of ``prompt``/``script``; a script loop carries an interval."""
        if bool(self.prompt) == bool(self.script):
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
