"""DB-backed per-loop enable/disable/pause/resume state machine (#1913).

One :class:`LoopState` row per loop name carries the durable control-plane
status of that mini-loop: ``enabled`` (the default — runs), ``paused`` (a
reversible hold), or ``disabled`` (a durable kill-switch). The state is the
canonical control tier, mirroring :class:`teatree.core.models.config_setting.ConfigSetting`
("the canonical tier is the DB", #1775 / §17.4): an **absent row resolves to
``ENABLED``**, so an empty table leaves every loop running exactly as it does
today. This is the SINGLE disable authority — loop control is ``/loops``
(``t3 loop enable``/``disable``/``pause``/``resume``) + the DB only; there is no env
kill-switch and no ``[loops]`` toml disabled-state fallback.

The motivation is the 2026-06-03 'pause everything' incident: there was no
single atomic command and no durable paused state that survived a session
restart. A row written here outlives the process — the tick AND the in-session
Stop self-pump both consult it, so a paused loop stays paused across a restart,
including the core ``dispatch`` loop.

Transitions are atomic single-row upserts (``update_or_create`` on the unique
``name``) so two racing writers cannot produce a duplicate row, and they are
idempotent — re-issuing the same transition leaves the one row in the target
status. ``resume`` and ``enable`` are the same "make it run again" transition
(both clear EITHER a pause or a disable) so a loop is never stuck because the
operator reached for the pause-vocabulary verb on a disabled loop.
"""

import datetime as dt
from typing import ClassVar

from django.db import models
from django.utils import timezone


class ForcedState(models.TextChoices):
    """The tri-state emergency FORCED plane over the preset mask (#3248).

    ``NEUTRAL`` (the default and the resolved state of any loop with no row)
    lets the preset/base decide. ``ON`` force-runs the loop even against a
    preset that forces it off; ``OFF`` force-skips it. A durable hold (PAUSED/
    DISABLED) still beats a FORCED value — resolution is hold > forced > preset
    > base. This is the emergency handle (``t3 loop override``), TTL-bounded via
    ``forced_until``; the per-loop enable/disable/pause/resume verbs are the
    normal handle only under ``--emergency``.
    """

    NEUTRAL = "neutral", "Neutral"
    ON = "on", "Forced on"
    OFF = "off", "Forced off"


def row_forced_value(
    forced: str | None, forced_until: dt.datetime | None, now: dt.datetime | None = None
) -> bool | None:
    """Resolve a row's stored forced plane to ``True``/``False``/``None``.

    NEUTRAL / absent → ``None``; an ON/OFF whose ``forced_until`` has passed →
    ``None`` (expired). The one place the TTL-expiry rule lives, shared by the
    single-lookup and bulk reads.
    """
    if forced in {ForcedState.ON.value, ForcedState.OFF.value}:
        if forced_until is not None and forced_until <= (now or timezone.now()):
            return None
        return forced == ForcedState.ON.value
    return None


class LoopStatus(models.TextChoices):
    """The three durable control-plane states of a mini-loop.

    ``ENABLED`` is the default (and the resolved status of any loop with no
    row): the loop runs subject only to its cadence (loop control is ``/loops``
    + the DB; there is no env kill-switch and no ``[loops]`` toml
    disabled-state fallback). ``PAUSED`` is a reversible hold; ``DISABLED`` a
    durable kill-switch. Only ``ENABLED`` is runnable — both other states skip
    the loop in the tick and suppress the self-pump.
    """

    ENABLED = "enabled", "Enabled"
    PAUSED = "paused", "Paused"
    DISABLED = "disabled", "Disabled"


class LoopStateManager(models.Manager["LoopState"]):
    """Read/transition surface for the per-loop control plane.

    The manager owns the absent-row → ``ENABLED`` fall-through contract and the
    atomic, idempotent transitions. Callers (the tick gate and the self-pump
    hook) ask only "is this loop runnable / paused / disabled?" and never touch
    the ``status`` string directly.
    """

    def status_of(self, name: str) -> LoopStatus:
        """Return the durable status of *name*, or ``ENABLED`` when no row exists.

        ``ENABLED`` is the fall-through default: an empty table leaves every
        loop resolving exactly as its cadence dictates (the
        #1913 no-regression invariant; #2702 removed the toml tier).
        """
        row = self.filter(name=name).first()
        if row is None:
            return LoopStatus.ENABLED
        return LoopStatus(row.status)

    def is_runnable(self, name: str) -> bool:
        """True iff *name* is in the ``ENABLED`` state (no pause, no disable)."""
        return self.status_of(name) is LoopStatus.ENABLED

    def held_names(self) -> set[str]:
        """Every loop name a durable ``PAUSED`` / ``DISABLED`` row holds — the tick's single bulk read.

        One ``values_list`` over the small control table, so the loop-table fan-out
        resolves every loop's hold from ONE query instead of a per-loop
        :meth:`is_runnable` round-trip (#2584 N+1). An absent name (no row) is
        ``ENABLED`` → not held, so it is simply not in the returned set.
        """
        return {name for name, status in self.values_list("name", "status") if status != LoopStatus.ENABLED.value}

    def is_paused(self, name: str) -> bool:
        return self.status_of(name) is LoopStatus.PAUSED

    def is_disabled(self, name: str) -> bool:
        return self.status_of(name) is LoopStatus.DISABLED

    def pause(self, name: str) -> "LoopState":
        """Atomically move *name* into the ``PAUSED`` hold (reversible)."""
        return self._set_status(name, LoopStatus.PAUSED)

    def disable(self, name: str) -> "LoopState":
        """Atomically move *name* into the durable ``DISABLED`` kill-switch."""
        return self._set_status(name, LoopStatus.DISABLED)

    def resume(self, name: str) -> "LoopState":
        """Return *name* to ``ENABLED``, clearing EITHER a pause or a disable.

        ``resume`` and :meth:`enable` are the one "make it run again"
        transition — a single verb must lift either hold so a disabled loop is
        never stuck because the operator used the pause-vocabulary verb.
        """
        return self._set_status(name, LoopStatus.ENABLED)

    def enable(self, name: str) -> "LoopState":
        """Return *name* to ``ENABLED`` (alias of :meth:`resume`)."""
        return self._set_status(name, LoopStatus.ENABLED)

    def _set_status(self, name: str, status: LoopStatus) -> "LoopState":
        """Atomic, idempotent upsert of *name*'s status to *status*.

        ``update_or_create`` on the unique ``name`` makes this a single-row
        last-write-wins transition: re-issuing the same status is a no-op that
        keeps exactly one row, and a concurrent writer cannot fork a duplicate.
        """
        row, _ = self.update_or_create(name=name, defaults={"status": status.value})
        return row

    def override(self, name: str, *, on: bool, until: dt.datetime | None = None, reason: str = "") -> "LoopState":
        """Set the emergency FORCED value for *name* (``on`` → run, else skip).

        Writes only the forced plane — the hold ``status`` is left untouched, so
        an override never clears a PAUSE/DISABLE. ``until`` bounds the override
        (an expired TTL resolves back to neutral); ``reason`` is a free-form note.
        """
        row, _ = self.update_or_create(
            name=name,
            defaults={
                "forced": (ForcedState.ON if on else ForcedState.OFF).value,
                "forced_until": until,
                "forced_reason": reason,
            },
        )
        return row

    def clear_override(self, name: str) -> "LoopState":
        """Return *name*'s forced plane to NEUTRAL (the hold ``status`` is untouched)."""
        row, _ = self.update_or_create(
            name=name,
            defaults={"forced": ForcedState.NEUTRAL.value, "forced_until": None, "forced_reason": ""},
        )
        return row

    def forced_of(self, name: str, now: dt.datetime | None = None) -> bool | None:
        """The live forced verdict for *name*: ``True``/``False``/``None`` (neutral).

        An absent row, a NEUTRAL row, or an EXPIRED TTL all resolve to ``None``.
        """
        row = self.filter(name=name).first()
        if row is None:
            return row_forced_value(None, None, now)
        return row_forced_value(row.forced, row.forced_until, now)

    def forced_map(self, now: dt.datetime | None = None) -> dict[str, bool]:
        """Every loop name with a LIVE (non-neutral, un-expired) forced value — the tick's bulk read.

        One ``values_list`` over the small control table (mirroring
        :meth:`held_names`), so the loop-table fan-out resolves every loop's
        forced value from ONE query instead of a per-loop lookup.
        """
        _, forced = self.control_planes(now)
        return forced

    def control_planes(self, now: dt.datetime | None = None) -> "tuple[set[str], dict[str, bool]]":
        """The (held names, live forced map) pair in ONE ``values_list`` — the tick's single control read.

        The hold plane (PAUSED/DISABLED) and the emergency FORCED plane are both
        resolved from one query so the per-tick admission (#2584 N+1) issues a
        single ``teatree_loop_state`` read for both planes rather than one each.
        """
        moment = now or timezone.now()
        held: set[str] = set()
        forced: dict[str, bool] = {}
        for name, status, forced_value, until in self.values_list("name", "status", "forced", "forced_until"):
            if status != LoopStatus.ENABLED.value:
                held.add(name)
            value = row_forced_value(forced_value, until, moment)
            if value is not None:
                forced[name] = value
        return held, forced


class LoopState(models.Model):
    """One row per mini-loop name carrying its durable control-plane status."""

    name = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=LoopStatus, default=LoopStatus.ENABLED)
    # The emergency FORCED plane (#3248) — orthogonal to the hold ``status``.
    forced = models.CharField(max_length=16, choices=ForcedState, default=ForcedState.NEUTRAL)
    forced_until = models.DateTimeField(null=True, blank=True)
    forced_reason = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[LoopStateManager] = LoopStateManager()

    class Meta:
        db_table = "teatree_loop_state"
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return f"loop-state<{self.name}={self.status}>"
