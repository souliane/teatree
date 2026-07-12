"""Shared leaf: bootstrap Django once per hook subprocess.

The hook subprocess never calls ``django.setup()`` on its own, yet several
gate handlers resolve overlays / models through the app registry. This
self-contained utility (src-path insert + ``django.setup()`` + bool return)
has ZERO dependency on ``hook_router``, so both ``hook_router`` and the sibling
gate modules import it directly without a cycle.
"""

import os
import sys
from pathlib import Path


def bootstrap_teatree_django() -> bool:
    """Import teatree and run ``django.setup()`` once per hook process.

    Returns ``True`` when the bootstrap succeeded (the away-mode handler
    can record a ``DeferredQuestion`` row) and ``False`` when ``teatree``
    is unavailable (the handler then fails open — never intercepts).
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        import django  # noqa: PLC0415 — deferred: Django import at call time

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        django.setup()
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False
    return True
