"""DB-backed per-loop enable/disable/pause/resume state machine (#1913).

One :class:`LoopState` row per loop name carries the durable control-plane
status of that mini-loop: ``enabled`` (the default — runs), ``paused`` (a
reversible hold), or ``disabled`` (a durable kill-switch). The state is the
canonical control tier, mirroring :class:`teatree.core.models.config_setting.ConfigSetting`
("the canonical tier is the DB", #1775 / §17.4): an **absent row resolves to
``ENABLED``**, so an empty table leaves every loop running exactly as it does
today. This is the SINGLE disable authority — loop control is ``/loops``
(``t3 loop enable/disable/pause/resume``) + the DB only; there is no env
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

from typing import ClassVar

from django.db import models


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


class LoopState(models.Model):
    """One row per mini-loop name carrying its durable control-plane status."""

    name = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=LoopStatus, default=LoopStatus.ENABLED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[LoopStateManager] = LoopStateManager()

    class Meta:
        db_table = "teatree_loop_state"
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return f"loop-state<{self.name}={self.status}>"
