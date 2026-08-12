"""Filesystem probe for a skill path, safe to run on a name the hook did not choose.

A bare sibling module (like ``mr_cli_fields`` / ``django_bootstrap``): the router
puts the plugin root on ``sys.path`` at import, so ``hooks.scripts.skill_path_probe``
resolves both as the live hook and in tests. It NEVER imports the router back.
"""

from pathlib import Path


def is_file_safe(path: Path) -> bool:
    """``path.is_file()`` that returns ``False`` instead of raising ``OSError``.

    A 255+ byte path segment makes ``is_file`` raise ``OSError`` ("File name too
    long"), and the names probed here come from ``<session>.pending``, which no
    gate authored. Degrading to "absent" makes such a name unresolvable — which
    the skill-loading gate already fails OPEN on — instead of propagating.
    """
    try:
        return path.is_file()
    except OSError:
        return False
