"""Reading the ``ConfigSetting`` override rows — and telling a FAILED read from an empty one.

Split out of :mod:`teatree.config.resolution` for the module-health LOC cap: ``resolution``
owns the resolution ORDER (which tier layers onto which, the pin sets, the autonomy
collapse), this module owns the one question underneath it — what the two DB scopes
actually contain, and whether that answer is trustworthy.

That second half is #3873. Both readers return ``(rows, degraded)`` rather than a bare
dict, because ``{}`` alone cannot carry the difference between "this scope has no
overrides" and "this scope could not be read": returning the same value for both is what
let a contended SQLite read silently drop every operator override and resolve the safety
gates to shipped defaults. A genuine BOOTSTRAP state stays silent and undegraded (it is a
no-op by construction); a runtime fault is retried, then reported loud AND recorded.

Django-free at module top — ``teatree.config``'s package init imports ``resolution``, which
imports this, and the cold hook path imports that package, so every Django touch is a
call-time import.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

from teatree.config.override_read_health import record_degraded_read
from teatree.config.settings import OverlayEntry

if TYPE_CHECKING:
    from collections.abc import Callable

_logger = logging.getLogger("teatree.config")

#: The two DB scope labels a degraded read is recorded under. Stable identifiers, not prose:
#: the doctor check and ``SettingLayers.degraded_scopes`` both key off them, so the overlay
#: label carries no overlay NAME (the name goes in the log line instead).
GLOBAL_SCOPE_LABEL = "global"
OVERLAY_SCOPE_LABEL = "overlay"


def _app_registry_ready() -> bool:
    """True when Django is configured AND its app registry is fully populated (post-``django.setup()``)."""
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time
    from django.conf import settings as django_settings  # noqa: PLC0415 — deferred: settings read at call time

    return django_settings.configured and apps.ready


def _override_read_degrades_silently(exc: BaseException) -> bool:
    """Whether a caught override-read exception is a genuine BOOTSTRAP no-op (silent ``{}``).

    ``ImproperlyConfigured`` / ``AppRegistryNotReady`` are unambiguous bootstrap states —
    always silent. ``OperationalError`` / ``ProgrammingError`` are AMBIGUOUS: a bootstrap
    signal (missing table, DB not ready) before ``django.setup()``, but ALSO a real RUNTIME
    fault (a locked SQLite DB, a lock timeout, a mid-session drop) once the registry is
    ready — the TYPE alone can't tell them apart, so they are silent ONLY while the registry
    is not ready (:func:`_app_registry_ready`); a runtime one logs loud. Any OTHER exception
    is a real read bug — always loud.
    """
    from django.core.exceptions import (  # noqa: PLC0415 — deferred: Django import at call time
        AppRegistryNotReady,
        ImproperlyConfigured,
    )
    from django.db.utils import (  # noqa: PLC0415 — deferred: Django import at call time
        OperationalError,
        ProgrammingError,
    )

    if isinstance(exc, ImproperlyConfigured | AppRegistryNotReady):
        return True
    if isinstance(exc, OperationalError | ProgrammingError):
        return not _app_registry_ready()
    return False


# The loud SIGNAL for a non-bootstrap ``ConfigSetting`` read fault. Such a failure is a
# fail-OPEN of the ENTIRE DB override tier — it drops the ``autonomy`` /
# ``require_human_approval_to_merge`` safety gates back to the dataclass defaults — so it
# is logged ``ERROR`` + traceback (the "raise or log-and-signal, not SILENTLY fail-open"
# contract) rather than swallowed: operator error-monitoring surfaces the real fault.
_OVERRIDE_READ_FAILURE_MSG = (
    "ConfigSetting %s-scope override read FAILED unexpectedly — resolving with NO DB override tier for this "
    "read (safety gates fall back to dataclass defaults). This is a real read fault, not a bootstrap no-op; "
    "fix the DB/read error."
)


#: How many times ONE scope read is attempted before the tier is declared degraded, and the
#: pause before each retry. The fault this exists for is CONTENTION — a SQLite lock held by
#: a concurrent writer — which is transient by construction, so the first exception is not
#: yet evidence that the tier is unreadable. The budget is small and BOUNDED on purpose: a
#: retry that hides a persistent fault is worse than the fail-open it replaced, so the
#: attempts are exhausted quickly and the degradation is then recorded rather than waited
#: out. Total added latency on the failure path is the sum of :data:`_READ_RETRY_BACKOFF`.
_READ_ATTEMPTS = 3
_READ_RETRY_BACKOFF: tuple[float, ...] = (0.05, 0.15)


def _read_scope_rows(
    scope_label: str, read: "Callable[[], dict[str, Any]]", *, log_label: str = ""
) -> tuple[dict[str, Any], bool]:
    """Run *read* with the bounded retry, returning ``(rows, degraded)``.

    ``degraded`` is the distinction #3873 exists for: ``({}, False)`` is a healthy read of
    a tier with no rows, ``({}, True)`` is a tier whose content could not be determined.
    They are the same dict and must never be the same answer.

    A genuine bootstrap state (:func:`_override_read_degrades_silently`) is neither retried
    nor reported — it is a no-op by construction, and retrying it would put the backoff on
    every cold-start read. Any other exception is a real fault: retried while the budget
    lasts, then logged loud (:data:`_OVERRIDE_READ_FAILURE_MSG`) and RECORDED where an
    operator can see it without reading this log.
    """
    for attempt in range(_READ_ATTEMPTS):
        try:
            return read(), False
        except Exception as exc:
            if _override_read_degrades_silently(exc):
                return {}, False
            if attempt + 1 >= _READ_ATTEMPTS:
                _logger.exception(_OVERRIDE_READ_FAILURE_MSG, log_label or scope_label)
                record_degraded_read(scope_label)
                return {}, True
            time.sleep(_READ_RETRY_BACKOFF[min(attempt, len(_READ_RETRY_BACKOFF) - 1)])
    return {}, True  # pragma: no cover — the loop always returns


def load_global_rows() -> tuple[dict[str, Any], bool]:
    """Read the GLOBAL-scope (``scope=""``) rows as ``(rows, degraded)``.

    Reaches the model via Django's app registry (no static ``teatree.core`` import — that
    would be a backwards ``platform -> domain`` tach edge). A genuine bootstrap state
    degrades SILENTLY (:func:`_override_read_degrades_silently`); a RUNTIME fault — incl.
    an ``OperationalError`` / ``ProgrammingError`` raised while the app registry is ready —
    is retried, then reported as ``degraded=True`` rather than as an empty tier.
    """
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

    def read() -> dict[str, Any]:
        model = apps.get_model("core", "ConfigSetting")
        return dict(model.objects.overrides_for_scope(""))

    return _read_scope_rows(GLOBAL_SCOPE_LABEL, read)


def load_overlay_rows(overlay_name: str = "") -> tuple[dict[str, Any], bool]:
    """Read the active overlay's ``{key: value}`` rows, alias-tolerant, or ``{}``.

    Matches the row's scope to *overlay_name* canonical-alias-tolerantly (a row
    under either the short alias or the ``t3-``-prefixed entry-point name resolves
    for the active overlay) and MERGES every canonically-equivalent scope group —
    a row scoped ``myovl`` and one scoped ``t3-myovl`` both apply. Alias groups
    apply in sorted-scope order, then the exact-name group last, so on a key
    collision the exact-name row wins. Same signal-on-real-failure posture as
    :func:`load_global_rows`: a genuine bootstrap state is silent, a runtime fault
    (incl. a ready-registry ``OperationalError`` / ``ProgrammingError``) logs loud.
    """
    if not overlay_name:
        return {}, False
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

    def read() -> dict[str, Any]:
        model = apps.get_model("core", "ConfigSetting")
        canonical = OverlayEntry.canonical_overlay_name(overlay_name)
        scope_values: dict[str, dict[str, Any]] = {}
        for scope, key, value in model.objects.exclude(scope="").values_list("scope", "key", "value"):
            if scope == overlay_name or OverlayEntry.canonical_overlay_name(scope) == canonical:
                scope_values.setdefault(scope, {})[key] = value
        merged: dict[str, Any] = {}
        for scope in sorted(scope_values):
            if scope != overlay_name:
                merged.update(scope_values[scope])
        merged.update(scope_values.get(overlay_name, {}))
        return merged

    return _read_scope_rows(OVERLAY_SCOPE_LABEL, read, log_label=f"overlay {overlay_name!r}")


__all__ = [
    "GLOBAL_SCOPE_LABEL",
    "OVERLAY_SCOPE_LABEL",
    "load_global_rows",
    "load_overlay_rows",
]
