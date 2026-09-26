"""Every preset holds an opinion on every loop — the totality invariant (B1).

The tri-state died here. A preset entry used to be ``True`` / ``False`` / *absent*,
and absent meant "inherit ``Loop.enabled``" — so what a preset did depended on a
column the preset could not see, and a loop introduced after the preset was written
silently inherited whatever its own row said. ``maintenance`` masked delivery off,
named no opinion on ``issue_implementer``, and the factory spent thirteen hours a
night claiming issues nothing could merge.

A total preset answers for itself: read one row, read one key, done. A loop the map
does not name is OFF (:meth:`teatree.core.models.loop_preset.Mode.state_for`), and a
loop born after the map was written is written into every preset as ``False`` by
:meth:`teatree.core.models.loop_preset.ModeManager.backfill_loop` — a new loop starts
quiet in every posture until someone admits it deliberately.

Two functions, one derivation. :func:`totalized_entries` REPAIRS — every write seam
folds its edit through it, so totality holds by construction and no operator can be
locked out by a row an older teatree wrote. :func:`require_total_entries` REFUSES —
it guards the one surface that writes the JSON directly, the Django admin, through
``Mode.clean()``.
"""

from collections.abc import Mapping

from teatree.core.models.loop import Loop

__all__ = ["PresetNotTotalError", "live_loop_names", "require_total_entries", "totalized_entries"]


class PresetNotTotalError(ValueError):
    """A preset map was written without an opinion on every live loop."""


def live_loop_names() -> set[str]:
    return set(Loop.objects.values_list("name", flat=True))


def totalized_entries(entries: object, *, loop_names: set[str] | None = None) -> dict[str, bool]:
    """*entries* as an exact, all-bool opinion over every live loop.

    Missing and non-bool entries become ``False``; keys naming no live loop are dropped.
    """
    names = live_loop_names() if loop_names is None else loop_names
    stored = entries if isinstance(entries, Mapping) else {}
    return {name: stored.get(name) is True for name in sorted(names)}


def require_total_entries(entries: object, *, preset_name: str, loop_names: set[str] | None = None) -> dict[str, bool]:
    """*entries* unchanged when it already answers for every live loop, else raise."""
    names = live_loop_names() if loop_names is None else loop_names
    stored = entries if isinstance(entries, Mapping) else {}
    non_bool = sorted(str(key) for key, value in stored.items() if not isinstance(value, bool))
    missing = sorted(names - set(stored))
    stale = sorted(str(key) for key in set(stored) - names)
    if missing or stale or non_bool:
        raise PresetNotTotalError(_refusal(preset_name, missing=missing, stale=stale, non_bool=non_bool))
    return {str(key): bool(value) for key, value in stored.items()}


def _refusal(preset_name: str, *, missing: list[str], stale: list[str], non_bool: list[str]) -> str:
    parts = []
    if missing:
        parts.append(f"no opinion on {', '.join(missing)}")
    if stale:
        parts.append(f"names no live loop: {', '.join(stale)}")
    if non_bool:
        parts.append(f"non-boolean entries: {', '.join(non_bool)}")
    return (
        f"preset {preset_name!r} must hold a true/false opinion on every loop — {'; '.join(parts)}. "
        "Set the missing loops explicitly (`t3 loop preset edit <preset> --set <loop>=off`) and drop the stale keys."
    )
