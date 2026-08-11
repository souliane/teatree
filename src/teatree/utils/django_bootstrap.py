"""The single sanctioned ``django.setup()`` entry point for Typer commands.

Django-free Typer groups (``teatree.cli``) are reachable before Django is
configured, so any command body that touches the ORM must bootstrap Django
first. Doing that inline (``import django`` + ``DJANGO_SETTINGS_MODULE``
setdefault + ``django.setup()``) had drifted across 30+ call sites under two
private wrapper names; :func:`ensure_django` is the one place that owns it.

``django.setup`` is registered as a ``module_attr`` chokepoint
(``quality/chokepoints.yaml``) with this module as the sole allowed caller, so
a new inline ``django.setup()`` anywhere else fails the chokepoint hook.

``django.setup()`` is a no-op only once it has *finished*. ``Apps.populate``
sets ``loading = True`` under no ``try``/``finally``, so a setup that dies
partway leaves the registry stalled for the life of the process and every later
bootstrap meets Django's reentrancy guard instead. Teatree reaches that state
through its own fail-safe call sites (``cli/config_view.py``,
``cli/push_gate_tools.py``, ``cli/ci.py``, ``cli/agent.py``), which swallow the
first failure — so the CLI reported ``populate() isn't reentrant`` with the real
cause gone (souliane/teatree#4207).
"""

import inspect
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import FrameType

    from django.apps.registry import Apps

_SETTINGS_MODULE = "teatree.settings"


class DjangoBootstrapStalledError(RuntimeError):
    """An earlier ``django.setup()`` failed and left the app registry unusable.

    A ``RuntimeError`` so the fail-safe handlers already catching Django's own
    reentrancy error keep behaving as they did.
    """

    def __init__(self, cause: BaseException | None) -> None:
        origin = (
            "Its cause is chained below."
            if cause is not None
            else "A caller swallowed that failure, so its cause is not recorded."
        )
        super().__init__(
            "Django's app registry is stalled: an earlier django.setup() in this process failed "
            f"partway and left it loading. {origin} Django will not re-run AppConfig.ready(), so "
            "this process cannot recover — fix that failure, or rerun in a fresh process."
        )


class _Bootstrap:
    """The first failure to escape our own ``django.setup()``, kept as the cause."""

    first_failure: BaseException | None = None


def _populate_frame_is_live(registry: "Apps") -> bool:
    """Is this call already inside the ``populate()`` that will configure ``registry``?

    A frame stack is per-thread, so this answers only for the caller: a *different*
    thread mid-populate leaves this False and the bootstrap blocks on Django's own
    lock instead, which is the case Django already handles correctly.
    """
    from django.apps.registry import Apps  # noqa: PLC0415 — deferred: keeps CLI startup Django-free

    populating = Apps.populate.__code__
    frame: FrameType | None = inspect.currentframe()
    while frame is not None:
        if frame.f_code is populating and frame.f_locals.get("self") is registry:
            return True
        frame = frame.f_back
    return False


def ensure_django() -> None:
    """Configure Django once so an ORM-touching command body can run.

    Idempotent: ``DJANGO_SETTINGS_MODULE`` is set with ``setdefault`` and
    ``django.setup()`` is a no-op after a completed first call, so repeated
    invocation across nested command dispatch is safe.
    """
    import django  # noqa: PLC0415 — deferred: Django import at call time
    from django.apps import apps  # noqa: PLC0415 — deferred: paired with the Django import above

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", _SETTINGS_MODULE)

    if _populate_frame_is_live(apps):
        return

    stalled = apps.loading and not apps.ready
    try:
        django.setup()
    except Exception as exc:
        if stalled and isinstance(exc, RuntimeError):
            raise DjangoBootstrapStalledError(_Bootstrap.first_failure) from (_Bootstrap.first_failure or exc)
        if _Bootstrap.first_failure is None:
            _Bootstrap.first_failure = exc
        raise


__all__ = ["DjangoBootstrapStalledError", "ensure_django"]
