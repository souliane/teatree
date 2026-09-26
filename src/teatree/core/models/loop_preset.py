"""Loop presets + the manual override row — the read-time mask layer (#3159).

A :class:`Mode` is a named, owner-editable, DB-stored loop-state configuration: its
``entries`` map is a TOTAL boolean opinion per loop (B1) — every live loop named,
``true`` = runs, ``false`` = does not. There is no absent tier and nothing to inherit,
so what a preset does is readable from the preset alone. Totality is repaired at every
write seam and refused in :meth:`Mode.clean` (:mod:`teatree.core.models.preset_totality`).
Presets never rewrite ``Loop``/``LoopState`` rows — a preset becomes effective only as a
read-time mask, resolved below a ``LoopState`` hold and the manual override
(:mod:`teatree.loop.preset_resolution`). Loops are referenced **by name** (JSON map keys,
not FKs): a key naming no live loop is dropped at the next write and surfaced by
``t3 doctor``, exactly as :class:`teatree.core.models.loop_state.LoopState` does.

``egress`` is the posture's opinion on acting OUTWARD on the owner's behalf. It is the
DECISION for the loop layer, which reads it at SELECTION time so a loop never starts work
whose output would be discarded (B14); the on-behalf chokepoint reads it too, but only as a
backstop, and a post reaching that backstop is a bug report about a loop that was not split
(B15).

**The target shape (B17), so the next change sees it:**

    schedule  -->  preset  -->  loops  -->  loop settings
                        +-->  settings

Schedules decide presets; presets decide loops AND settings; loops decide their own
settings. Time enters in exactly ONE place — the schedule — so everything below it is a
static declaration.

Which edges exist TODAY: schedule -> preset and preset -> loops are built. **preset ->
settings is NOT.** ``egress`` is the one setting-shaped opinion a preset carries and it is a
bespoke column, not the general payload B16 describes. Three things that mechanism needs and
that do not exist yet:

1.  the per-setting "is this schedule-varying?" declaration (B11's taxonomy) — without it,
    a preset that can touch any setting is unbounded magic;
2.  a resolver that can say WHY a value resolved as it did
    (``env -> manual override -> preset -> DB -> code default -> defaults.toml``, where a
    MANUAL override beats the preset exactly as it does for loops: the preset never silently
    wins over a human decision, it notifies and keeps, A7/A8 unchanged);
3.  an explicit ORIGIN marker beside each stored value (``manual`` / ``seeded`` /
    ``inherited``). A setting cannot borrow the loop tri-state's trick: ``Loop.enabled``'s
    domain is boolean so ``None`` is spare, while a setting's domain is OPEN —
    ``token_outage_preset_name`` ships empty on purpose — so any sentinel collides with real
    data, silently, via whichever setting legitimately holds that value.

A :class:`ModeOverride` (≤1 row) is the manual layer above the schedule: a preset the
owner activated by hand, carrying the REASON it was set. It never expires — an override
nobody lifted after the incident is worse than one that vanished on a timer, so the row
stands until a person clears it and ``expected_lift_at`` is advisory (A5/A7). It stores
the preset **by name** so a deleted preset fails open rather than cascading.
"""

import logging
from datetime import datetime
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models, transaction

from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.preset_totality import PresetNotTotalError, require_total_entries

logger = logging.getLogger(__name__)

# Token-outage auto-engage (#3159 build item 6): default-OFF flag + re-pointable target.
TOKEN_OUTAGE_AUTO_ENGAGE_SETTING = "token_outage_auto_engage"  # noqa: S105 — a setting key, not a credential
TOKEN_OUTAGE_PRESET_SETTING = "token_outage_preset_name"  # noqa: S105 — a setting key, not a credential
DEFAULT_TOKEN_OUTAGE_PRESET = "token-outage"  # noqa: S105 — a preset name, not a credential
# Marks an override this system engaged automatically (vs. one the user set), so
# the re-arm path clears only its OWN override and never a user's.
_AUTO_TOKEN_OUTAGE_REASON = "auto:token-outage (usage window parked)"  # noqa: S105 — an audit reason, not a credential


class ModeManager(models.Manager["Mode"]):
    def by_name(self, name: str) -> "Mode | None":
        return self.filter(name=name).first()

    def backfill_loop(self, loop_name: str) -> int:
        """Write ``False`` for *loop_name* into every preset with no opinion on it.

        A loop is quiet in every posture at birth: admitting it anywhere is a deliberate
        edit, never a side effect of the row appearing.
        """
        backfilled = 0
        for preset in self.all():
            stored = preset.entries if isinstance(preset.entries, dict) else {}
            if isinstance(stored.get(loop_name), bool):
                continue
            preset.entries = {**stored, loop_name: False}
            preset.save(update_fields=["entries", "updated_at"])
            backfilled += 1
        return backfilled


class Egress(models.TextChoices):
    """Whether a posture may act OUTWARD on the owner's behalf.

    ``FORBID`` is the AFK line: implement, take intake, answer inbound review comments —
    but post nothing under the owner's identity to a colleague surface, because the owner
    is not there to reply to what comes back.
    """

    ALLOW = "allow", "Acts on the owner's behalf"
    FORBID = "forbid", "Never posts on the owner's behalf"


class Mode(models.Model):
    """One named operating **mode** — a total per-loop on/off table plus its egress posture.

    The five shipped modes (``present`` / ``afk`` / ``maintenance`` / ``token-outage`` /
    ``off``) are postures, not intensities: no nesting invariant holds between them.
    """

    name = models.SlugField(max_length=64, unique=True)
    description = models.TextField(blank=True, default="")
    entries = models.JSONField(default=dict)
    egress = models.CharField(max_length=16, choices=Egress, default=Egress.ALLOW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[ModeManager] = ModeManager()

    class Meta:
        db_table = "teatree_loop_preset"
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return f"loop-preset<{self.name} ({self.entry_count} entries)>"

    def clean(self) -> None:
        """Refuse a partial table — the guard on the one surface that writes ``entries`` raw.

        Every other write folds through
        :func:`teatree.core.models.preset_totality.totalized_entries` and cannot produce a
        partial row; the Django admin edits the JSON directly.
        """
        try:
            require_total_entries(self.entries, preset_name=self.name)
        except PresetNotTotalError as error:
            raise ValidationError({"entries": str(error)}) from error

    @property
    def forbids_egress(self) -> bool:
        return self.egress == Egress.FORBID

    def state_for(self, loop_name: str) -> bool:
        """Does this mode run *loop_name*? A loop the table does not name does NOT run.

        A total table names every live loop, so a miss means the row predates the loop or
        predates totality — either way the fail-safe answer is off, said loudly.
        """
        value = self.entries.get(loop_name) if isinstance(self.entries, dict) else None
        if isinstance(value, bool):
            return value
        logger.warning("preset %r holds no boolean opinion on loop %r — reading it OFF", self.name, loop_name)
        return False

    @property
    def entry_count(self) -> int:
        return len(self.entries) if isinstance(self.entries, dict) else 0


class ModeOverrideManager(models.Manager["ModeOverride"]):
    def current(self) -> "ModeOverride | None":
        """The single override row, or ``None`` when nobody has set one."""
        return self.order_by("-set_at").first()

    def set_override(
        self, preset_name: str, *, reason: str, expected_lift_at: datetime | None = None
    ) -> "ModeOverride":
        """Replace any existing override with a single fresh row (the ≤1-row invariant).

        The reason is REQUIRED: without it nothing can judge whether the override still
        applies, so the only possible policy would be a timer (A8). *expected_lift_at* is
        advisory — the watcher reminds against it and nothing enforces it.

        The purge and the insert share one ``atomic`` block, which SQLite opens
        ``IMMEDIATE``: without it two writers each purge, each insert, and the
        table holds two rows that no constraint can express.
        """
        if not reason.strip():
            msg = f"a mode override on {preset_name!r} must carry the reason it was set"
            raise ValueError(msg)
        with transaction.atomic():
            self.all().delete()
            return self.create(preset_name=preset_name, expected_lift_at=expected_lift_at, reason=reason.strip())

    def clear(self) -> bool:
        deleted, _ = self.all().delete()
        return deleted > 0

    def auto_engage_token_outage(self, *, resets_at: datetime) -> bool:
        """Engage the token-outage preset until *resets_at* when a usage window parks (#3159 item 6).

        A no-op unless the default-off ``token_outage_auto_engage`` flag is on AND the
        re-pointable target preset (``token_outage_preset_name``, default ``token-outage``)
        exists. **Never overwrites an existing override** — a user ``--hold`` (or any
        live override) outranks — so it engages only when nothing is currently active.
        The override is marked auto-engaged so the re-arm path clears only its own.
        Returns ``True`` iff it engaged. The outranks-check and the write share one
        ``atomic`` block, so a ``--hold`` set between them is seen rather than purged.
        """
        if not _token_outage_auto_engage_enabled():
            return False
        preset_name = token_outage_preset_name()
        if Mode.objects.by_name(preset_name) is None:
            logger.warning("token_outage_auto_engage on but preset %r is absent — not engaging", preset_name)
            return False
        with transaction.atomic():
            if self.current() is not None:
                return False
            self.set_override(preset_name, expected_lift_at=resets_at, reason=_AUTO_TOKEN_OUTAGE_REASON)
        return True

    def clear_auto_engaged_token_outage(self) -> bool:
        """Clear an override THIS system auto-engaged on a park; leave a user override intact.

        Returns ``True`` iff an auto-engaged token-outage override was cleared. A user
        override (any other reason) is never touched — the re-arm must not undo an
        override the operator set by hand. Only the identified row is deleted: a
        table-wide purge takes any user override that landed beside it with it.
        """
        row = self.order_by("-set_at").first()
        if row is None or row.reason != _AUTO_TOKEN_OUTAGE_REASON:
            return False
        deleted, _ = self.filter(pk=row.pk).delete()
        return deleted > 0


class ModeOverride(models.Model):
    """The manual override — at most one row, pointing at a preset by name."""

    preset_name = models.CharField(max_length=64)
    #: When the owner EXPECTS to lift it — what the watcher reminds against, never a TTL.
    expected_lift_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField()
    set_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[ModeOverrideManager] = ModeOverrideManager()

    class Meta:
        db_table = "teatree_loop_preset_override"
        ordering: ClassVar = ["-set_at"]

    def __str__(self) -> str:
        lift = "" if self.expected_lift_at is None else f" lift by {self.expected_lift_at.isoformat()}"
        return f"loop-preset-override<{self.preset_name}{lift}>"


def _token_outage_auto_engage_enabled() -> bool:
    return bool(ConfigSetting.objects.get_effective(TOKEN_OUTAGE_AUTO_ENGAGE_SETTING))


def token_outage_preset_name() -> str:
    """The mode the token-budget escape points at — only token-free loops stay up under it."""
    value = ConfigSetting.objects.get_effective(TOKEN_OUTAGE_PRESET_SETTING)
    return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_TOKEN_OUTAGE_PRESET
