"""Shared leaf: bootstrap Django once per hook subprocess.

The hook subprocess never calls ``django.setup()`` on its own, yet several
gate handlers resolve overlays / models through the app registry. This
self-contained utility (src-path insert + ``django.setup()`` + bool return)
has ZERO dependency on ``hook_router``, so both ``hook_router`` and the sibling
gate modules import it directly without a cycle.

A FAILED bootstrap is audible. ``run-hook.sh`` deliberately falls back to a
version-floor-only interpreter with no Django and prints nothing, so without a
line here every ORM-backed gate degrades to a silent allow that an operator
reading the session cannot tell from a clean pass. The line is emitted once per
process (the gates call this repeatedly) and names the missing capability, not
the gate — each degraded gate says SKIPPED itself, through
``gate_result.warn_gate_skipped``.
"""

import os
import sys
from pathlib import Path

#: Whether this process already named the missing capability. The bootstrap is
#: called by every ORM-backed gate in the chain; one line is the signal, ten is
#: noise the operator learns to skip past.
_MISSING_CAPABILITY_WARNED = False


def _django_setup() -> None:
    """Import Django and initialise the app registry against teatree's settings."""
    import django  # noqa: PLC0415 — deferred: Django import at call time

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    django.setup()


def _warn_missing_capability(exc: BaseException) -> None:
    """Name the missing capability on stderr, once per process."""
    global _MISSING_CAPABILITY_WARNED  # noqa: PLW0603 — process-scoped dedup for a stderr line
    if _MISSING_CAPABILITY_WARNED:
        return
    _MISSING_CAPABILITY_WARNED = True
    sys.stderr.write(
        f"NOTE: the hook interpreter cannot import Django ({type(exc).__name__}: {exc}). "
        "Every ORM-backed gate in this hook process is SKIPPED, not passed — each one "
        "allows its call unvalidated. Point the hooks at an interpreter with teatree's "
        "dependencies installed to restore them.\n"
    )


def bootstrap_teatree_django() -> bool:
    """Import teatree and run ``django.setup()`` once per hook process.

    Returns ``True`` when the bootstrap succeeded (the away-mode handler
    can record a ``DeferredQuestion`` row) and ``False`` when ``teatree``
    is unavailable — the handler then fails open (never intercepts) and the
    missing capability is named on stderr (:func:`_warn_missing_capability`).
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        _django_setup()
    except Exception as exc:  # noqa: BLE001 — crash-proof hook: any failure degrades, never breaks the tool call
        _warn_missing_capability(exc)
        return False
    return True
