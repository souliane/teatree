"""The single sanctioned ``django.setup()`` entry point for Typer commands.

Django-free Typer groups (``teatree.cli``) are reachable before Django is
configured, so any command body that touches the ORM must bootstrap Django
first. Doing that inline (``import django`` + ``DJANGO_SETTINGS_MODULE``
setdefault + ``django.setup()``) had drifted across 30+ call sites under two
private wrapper names; :func:`ensure_django` is the one place that owns it.

``django.setup`` is registered as a ``module_attr`` chokepoint
(``quality/chokepoints.yaml``) with this module as the sole allowed caller, so
a new inline ``django.setup()`` anywhere else fails the chokepoint hook.
"""

import os

_SETTINGS_MODULE = "teatree.settings"


def ensure_django() -> None:
    """Configure Django once so an ORM-touching command body can run.

    Idempotent: ``DJANGO_SETTINGS_MODULE`` is set with ``setdefault`` and
    ``django.setup()`` is a no-op after the first call, so repeated invocation
    across nested command dispatch is safe.
    """
    import django  # noqa: PLC0415 — deferred: Django import at call time

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", _SETTINGS_MODULE)
    django.setup()


__all__ = ["ensure_django"]
