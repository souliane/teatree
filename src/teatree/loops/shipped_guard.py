"""A typed phrase naming what stops, before a SHIPPED definition is deleted (#3842).

Soft protection rather than prohibition, for three reasons the measured data supports:
deletion is already self-healing (the seed is ``get_or_create`` by name, so ``t3 setup``
recreates a removed shipped row), a legitimate removal stays possible without a code
change, and refusal invites working around it — a hand-edited DB is strictly worse than an
audited delete. Nothing has ever actually been deleted on the live box; the failures that
cost time were present-but-inert, which is :mod:`teatree.loops.seed_inertness`'s half.

The phrase is ``stop-<name>`` rather than a generic ``DELETE``, and the refusal quotes the
shipped ``description``: the operator deleting a shipped loop is usually unclear on what it
does, so a refusal that names the consequence teaches more than one that just says no. Same
doctrine as ``stop-the-fleet`` (:data:`teatree.dash.loop_control.RUNNER_CONFIRM_PHRASE`),
whose dangerous direction is likewise OFF — nothing errors, work simply stops arriving.

The guard is a speed bump on a delete that is otherwise SAFE. It never overrides an
integrity refusal: a preset a schedule slot still names, or the active calendar, is refused
whatever phrase is typed.
"""

from pathlib import Path

from teatree.config.seed_defaults import is_shipped, shipped_description
from teatree.loops.preset_editing import PresetEditError

#: ``is_shipped`` is re-exported so a caller needing the predicate AND the policy imports
#: one module. The predicate itself lives in ``config`` because the Django admin consumes
#: it, and a ``domain``-layer module may not reach up into ``teatree.loops``.
__all__ = [
    "is_shipped",
    "require_shipped_delete_confirm",
    "shipped_delete_consequence",
    "shipped_delete_phrase",
]


def shipped_delete_phrase(name: str) -> str:
    """The exact phrase an operator must type — it NAMES what stops, never a generic token."""
    return f"stop-{name}"


def shipped_delete_consequence(family: str, name: str, path: Path | None = None) -> str:
    """The shipped one-line description of what *name* does, straight out of ``defaults.toml``."""
    return shipped_description(family, name, path)


def require_shipped_delete_confirm(family: str, name: str, confirm: str, path: Path | None = None) -> None:
    """Refuse deleting a shipped *name* unless *confirm* is exactly its phrase.

    An operator-created row is not shipped, so it needs no phrase — the friction lands only
    where an accidental delete silently stops work the box was configured to do.
    """
    if not is_shipped(family, name, path):
        return
    phrase = shipped_delete_phrase(name)
    if confirm.strip() == phrase:
        return
    msg = (
        f"refusing to delete shipped {family} {name!r} without confirmation. "
        f"It does: {shipped_delete_consequence(family, name, path)} "
        f"Deleting it stops that. Pass confirm={phrase!r} to delete it anyway "
        "(`t3 setup` recreates a deleted shipped row)."
    )
    raise PresetEditError(msg)
