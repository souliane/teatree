"""What a registry row means on the way back in — a compound row that survives redaction.

``overlays`` and ``e2e_repos`` are single ``ConfigSetting`` rows whose value is a table of
independent facts, and the export renders each entry as its own ``[overlays.<name>]`` /
``[e2e_repos.<name>]`` TOML table. That makes them the one place a file can be INCOMPLETE
about a row it describes: the secret guard withholds an overlay's credential coordinates
from a shared export, so the emitted table names the overlay without naming where its
tokens live.

Everywhere else the import already reads absence as silence — a setting the file never
mentions keeps its stored row untouched, and removing a value is ``config_setting clear``,
never a side effect of reading a file. Rebuilding a registry row from the file and storing
it wholesale broke that rule for the one row that is a table, which made the guard's
redaction the deletion: re-importing a box's own export destroyed the very credential
coordinates the guard had just protected (souliane/teatree#4147).
"""

from typing import Any

from teatree.config.stored_row_health import is_operator_configuration
from teatree.core.models.config_setting import ConfigValue


def merged_registry(incoming: dict[str, Any], stored: ConfigValue | None) -> dict[str, Any]:
    """*incoming* laid over *stored*, entry by entry and field by field.

    A field the file omits keeps what the store holds, so a redacted export re-imports as
    no change at all; a field the file carries wins, so editing the dump still edits the
    store. Dropping an overlay stays an explicit act on the store rather than something a
    file can do by not mentioning it — the same rule every other row already follows.
    """
    merged: dict[str, Any] = {
        name: dict(entry) if isinstance(entry, dict) else entry
        for name, entry in (stored.items() if isinstance(stored, dict) else ())
    }
    for name, entry in incoming.items():
        held = merged.get(name)
        if isinstance(held, dict) and isinstance(entry, dict):
            held.update(entry)
        else:
            merged[name] = entry
    return merged


def overlay_table_split(table: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """One ``[overlays.<name>]`` table split back into (setting rows, definition keys).

    The export JOINS an overlay's registry definitions with the per-overlay setting rows
    :func:`~teatree.config.stored_row_health.is_operator_configuration` recognises, so the
    inverse splits on that same predicate or the join is not reversible. Splitting on the
    ``UserSettings`` partition instead lost every setting outside it — ``agent_phase_harness``
    is one — into the definition registry, storing the value a second time under a meaning
    nothing reads while its own scoped row stayed behind holding the first.
    """
    settings = {key: value for key, value in table.items() if is_operator_configuration(key)}
    definitions = {key: value for key, value in table.items() if key not in settings}
    return settings, definitions


__all__ = ["merged_registry", "overlay_table_split"]
