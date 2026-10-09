"""Editing one SEED field — a loop's, preset's or schedule's own row — from a generic surface.

The shipped defaults an operator tunes are not only ``ConfigSetting`` keys: a loop's cadence
and description, a preset's entries and egress, a schedule's timezone are all seeded from
``defaults.toml`` and carried by ``config_setting export``/``import``. The comparison page
lists every one of them that two boxes disagree about, and until now could edit none — while
five of the nine interchange fields had no editor anywhere else in the dashboard either.

**A field a dedicated editor OWNS is not offered here.** A cadence pair is written through
``set_loop_cadence``, which holds the grid and the prompt/script invariant
:meth:`teatree.core.models.Loop.clean` refuses without; a preset's ``entries`` is written
through the preset editor, which repairs the totality every write seam guarantees. A second
writer reaching those columns raw would bypass the repair, so :data:`FIELD_OWNERS` keeps them
readings and NAMES the editor that owns them — the same shape the settings grid uses to say
why an env-pinned cell cannot be written.

Everything else routes through the interchange's own two seams:
:func:`~teatree.config.seed_defaults.classify_seed_field` decides whether the value may be
written at all, and :func:`~teatree.core.config_interchange.seed_tables.write_seed_field`
writes it. One validator and one writer for the import path and this page alike — a second
derivation is how "what may a seed field hold" drifts into two answers.
"""

import json
from typing import Final

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.urls import reverse

from teatree.config.seed_defaults import SEED_ROW_FIELDS, classify_seed_field
from teatree.core.config_interchange.seed_tables import write_seed_field

#: Seed fields a dedicated editor owns, and the sentence naming it. Read as: the generic
#: surface may not write this, because that editor holds an invariant a raw write skips.
FIELD_OWNERS: Final[dict[tuple[str, str], str]] = {
    ("loops", "delay_seconds"): "cadence is edited on the loops page, which holds the interval grid",
    ("loops", "daily_at"): "cadence is edited on the loops page, which holds the interval grid",
    ("modes", "entries"): "a preset's loop table is edited on the schedule and preset editor, which keeps it total",
}

_NOT_IN_INTERCHANGE: Final = "the interchange does not carry this field, so there is no row to write"
_NOT_SHIPPED: Final = "{table}.{name} is not carried by defaults.toml, so it cannot be restored here"
_NOT_PRESENT: Final = "{table}.{name} is not present on this instance, so there is no row to write"


def write_refusal(
    table: str,
    name: str,
    field: str,
    *,
    local_present: bool | None = None,
    shipped_entry: bool | None = None,
) -> str:
    """Why *field* cannot be written from a generic surface, or ``""`` — asked before rendering.

    Answered from the DECLARATIONS, never from whether a control happened to be built: a field
    that lost its control would otherwise go silently read-only instead of saying why.
    """
    if field not in SEED_ROW_FIELDS.get(table, {}):
        return _NOT_IN_INTERCHANGE
    if local_present is False:
        return _NOT_PRESENT.format(table=table, name=name)
    if owner := FIELD_OWNERS.get((table, field), ""):
        return owner
    if shipped_entry is False:
        return _NOT_SHIPPED.format(table=table, name=name)
    return ""


def write_url(table: str, name: str, field: str) -> str:
    """Where a seed cell's edit POSTs — the three coordinates that address one field."""
    return reverse("dash:settings_seed_set", args=[table, name, field])


def write_seed_cell(table: str, name: str, field: str, submitted: str) -> str:
    """Write *submitted* onto one seed field; ``""`` when it landed, else why it was refused.

    The submitted text is the JSON literal the cell holds, exactly as a settings cell's is, so
    the two surfaces stay one gesture. Refusals answer identically — the cell comes back
    carrying the reason — so they are decided here and the view is left with one branch.
    """
    if refusal := write_refusal(table, name, field):
        return refusal
    try:
        value = json.loads(submitted)
    except json.JSONDecodeError as exc:
        return f"invalid JSON value: {exc}"
    kind, reason = classify_seed_field(table, name, field, value)
    if kind == "reject":
        return f"invalid value for {table}.{name}.{field}: {reason}"
    # `skip` means only "equal to what the FILE ships", never "equal to what the ROW holds"
    # (souliane/teatree#4147). An operator moving a tuned row BACK to its shipped value is
    # exactly that case, so honouring the skip here would report a write that never happened.
    # The write is idempotent, so both dispositions take it.
    try:
        write_seed_field(table, name, field, value)
    except ValidationError as exc:
        return f"invalid value for {table}.{name}.{field}: {exc.messages[0]}"
    except ObjectDoesNotExist:
        return _NOT_PRESENT.format(table=table, name=name)
    return ""


__all__ = ["FIELD_OWNERS", "write_refusal", "write_seed_cell", "write_url"]
