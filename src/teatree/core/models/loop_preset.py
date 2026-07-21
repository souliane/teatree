"""Loop presets + the manual override row — the read-time mask layer (#3159).

A :class:`LoopPreset` is a named, owner-editable, DB-stored loop-state
configuration: its ``entries`` map is a **tri-state** opinion per loop
(``true`` = force on, ``false`` = force off, *absent* = inherit the base
``Loop.enabled``). Presets never rewrite ``Loop``/``LoopState`` rows — a preset
becomes effective only as a read-time mask, resolved above the base config and
below a ``LoopState`` hold (:mod:`teatree.loop.preset_resolution`). Loops are
referenced **by name** (JSON map keys, not FKs): a deleted or renamed loop leaves
an inert key that is ignored at read time and surfaced by ``t3 doctor``, exactly
as :class:`teatree.core.models.loop_state.LoopState` already references loops.

A :class:`LoopPresetOverride` (≤1 live row) is the manual L3 layer: a preset the
owner activated by hand, optionally with a TTL. It stores the preset **by name**
so a deleted preset fails open to base config rather than cascading.
"""

import logging
from datetime import datetime
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from teatree.core.models.config_setting import ConfigSetting

logger = logging.getLogger(__name__)

# The canonical availability-pin set — the modes a preset may pin. It is exactly
# the availability-mode set (``teatree.core.availability.VALID_MODES``); the two
# cannot share the constant because ``core.models`` is a domain leaf that must not
# import ``core.availability`` (a backwards tach edge), so equality is asserted by
# ``tests/teatree_core/models/test_loop_preset.py`` instead. Referenced by the
# model's ``availability_pin`` property and the ``loop_preset`` command's pin
# validator — the single source both consult (no more triplication).
PIN_MODES = frozenset({"present", "away", "autonomous_away"})

# Low-power auto-engage (#3159 build item 6): default-OFF flag + re-pointable target.
LOW_POWER_AUTO_ENGAGE_SETTING = "low_power_auto_engage"
LOW_POWER_PRESET_SETTING = "low_power_preset_name"
_DEFAULT_LOW_POWER_PRESET = "low-power"
# Marks an override this system engaged automatically (vs. one the user set), so
# the re-arm path clears only its OWN override and never a user's.
_AUTO_LOW_POWER_REASON = "auto:low-power (usage window parked)"


class LoopPresetManager(models.Manager["LoopPreset"]):
    def by_name(self, name: str) -> "LoopPreset | None":
        return self.filter(name=name).first()


class LoopPreset(models.Model):
    """One named operating **mode** (#61 merge).

    A tri-state per-loop opinion, an overlay scope, AND the intrinsic availability
    posture that used to live in the standalone :mod:`teatree.core.availability`
    string modes.

    The three booleans ARE the availability payload — a mode's reachability is
    fully expressed by them (the merge's key finding: availability adds no state a
    preset can't carry, only two booleans plus a presence rule):

    *   ``defers_questions`` — the user is unreachable NOW: ``AskUserQuestion``
        defers to the durable backlog, local TTS is silenced, colleague-facing
        loops are gated off, and returning to a non-deferring mode drains the
        backlog. Maps to the old ``away`` + ``autonomous_away`` modes.
    *   ``pauses_self_pump`` — stop self-driving too (holiday): the loop tick parks.
        Maps to the old ``away`` mode only. Requires ``defers_questions`` (the
        nonsensical "pump paused but questions answered" 4th point is unrepresentable).
    *   ``presence_sensitive`` — a fresh keystroke upgrades an away-class mode
        reached *by schedule/default* to the configured ``presence_upgrade_mode``.
        Defaults ``True`` so any scheduled away honours a live keystroke, exactly as
        the old presence rule did.

    The legacy ``availability_mode`` string is retained during the merge only to
    seed/back-fill the booleans and to keep the deprecation aliases working; it is
    scheduled for deletion once every consumer reads the booleans.
    """

    name = models.SlugField(max_length=64, unique=True)
    description = models.TextField(blank=True, default="")
    entries = models.JSONField(default=dict)
    availability_mode = models.CharField(max_length=32, blank=True, default="")
    defers_questions = models.BooleanField(default=False)
    pauses_self_pump = models.BooleanField(default=False)
    presence_sensitive = models.BooleanField(default=True)
    overlay_scope = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[LoopPresetManager] = LoopPresetManager()

    class Meta:
        db_table = "teatree_loop_preset"
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return f"loop-preset<{self.name} ({self.entry_count} entries)>"

    def clean(self) -> None:
        """A paused self-pump must also defer questions (§0.4 invariant).

        The reachable state space is the three points ``(F,F)`` present,
        ``(T,F)`` autonomous-away and ``(T,T)`` holiday-away; the fourth point
        ``(F,T)`` — the pump paused while questions still answer in-band — is
        nonsensical and rejected here (and asserted by a model test).
        """
        super().clean()
        if self.pauses_self_pump and not self.defers_questions:
            raise ValidationError(
                {"pauses_self_pump": "pauses_self_pump requires defers_questions (a paused pump must also defer)."}
            )

    def state_for(self, loop_name: str) -> bool | None:
        """The tri-state opinion for *loop_name*: ``True``/``False`` forced, ``None`` = inherit.

        A non-bool stored value (a corrupt row, a legacy string) reads as ``None``
        so a malformed entry degrades to inherit rather than forcing a verdict.
        """
        value = self.entries.get(loop_name) if isinstance(self.entries, dict) else None
        return value if isinstance(value, bool) else None

    @property
    def entry_count(self) -> int:
        return len(self.entries) if isinstance(self.entries, dict) else 0

    @property
    def availability_pin(self) -> str | None:
        """The availability mode this preset pins when active, or ``None`` for no pin."""
        mode = self.availability_mode.strip()
        return mode if mode in PIN_MODES else None

    @property
    def overlay_scope_names(self) -> list[str]:
        """The backend-name allowlist this preset restricts scanners to (``[]`` = all overlays)."""
        scope = self.overlay_scope
        if not isinstance(scope, list):
            return []
        return [entry for entry in scope if isinstance(entry, str) and entry]


class LoopPresetOverrideManager(models.Manager["LoopPresetOverride"]):
    def current(self, now: datetime | None = None) -> "LoopPresetOverride | None":
        """The single live override, or ``None`` when absent or expired.

        An expired row (``until`` passed) is inert — it is left for the
        transitions chain to reap, never treated as active here.
        """
        row = self.order_by("-set_at").first()
        if row is None:
            return None
        return row if row.is_active(now or timezone.now()) else None

    def set_override(
        self, preset_name: str, *, until: datetime | None = None, reason: str = ""
    ) -> "LoopPresetOverride":
        """Replace any existing override with a single fresh row (the ≤1-row invariant)."""
        self.all().delete()
        return self.create(preset_name=preset_name, until=until, reason=reason)

    def clear(self) -> bool:
        deleted, _ = self.all().delete()
        return deleted > 0

    def auto_engage_low_power(self, *, resets_at: datetime, now: datetime | None = None) -> bool:
        """Engage the low-power preset until *resets_at* when a usage window parks (#3159 item 6).

        A no-op unless the default-off ``low_power_auto_engage`` flag is on AND the
        re-pointable target preset (``low_power_preset_name``, default ``low-power``)
        exists. **Never overwrites an existing override** — a user ``--hold`` (or any
        live override) outranks — so it engages only when nothing is currently active.
        The override is marked auto-engaged so the re-arm path clears only its own.
        Returns ``True`` iff it engaged.
        """
        if not _low_power_auto_engage_enabled():
            return False
        preset_name = _low_power_preset_name()
        if LoopPreset.objects.by_name(preset_name) is None:
            logger.warning("low_power_auto_engage on but preset %r is absent — not engaging", preset_name)
            return False
        if self.current(now or timezone.now()) is not None:
            return False
        self.set_override(preset_name, until=resets_at, reason=_AUTO_LOW_POWER_REASON)
        return True

    def clear_auto_engaged_low_power(self) -> bool:
        """Clear an override THIS system auto-engaged on a park; leave a user override intact.

        Returns ``True`` iff an auto-engaged low-power override was cleared. A user
        override (any other reason) is never touched — the re-arm must not undo an
        override the operator set by hand.
        """
        row = self.order_by("-set_at").first()
        if row is None or row.reason != _AUTO_LOW_POWER_REASON:
            return False
        return self.clear()


class LoopPresetOverride(models.Model):
    """The manual L3 override — at most one live row, pointing at a preset by name."""

    preset_name = models.CharField(max_length=64)
    until = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    set_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[LoopPresetOverrideManager] = LoopPresetOverrideManager()

    class Meta:
        db_table = "teatree_loop_preset_override"
        ordering: ClassVar = ["-set_at"]

    def __str__(self) -> str:
        window = "hold" if self.until is None else f"until {self.until.isoformat()}"
        return f"loop-preset-override<{self.preset_name} {window}>"

    def is_active(self, now: datetime) -> bool:
        """``True`` while unexpired: a ``None`` ``until`` holds until cleared."""
        return self.until is None or now < self.until


def _low_power_auto_engage_enabled() -> bool:
    return bool(ConfigSetting.objects.get_effective(LOW_POWER_AUTO_ENGAGE_SETTING))


def _low_power_preset_name() -> str:
    value = ConfigSetting.objects.get_effective(LOW_POWER_PRESET_SETTING)
    return value.strip() if isinstance(value, str) and value.strip() else _DEFAULT_LOW_POWER_PRESET
