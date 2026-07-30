"""Overlay-code-default provider seam (#36).

A genuinely-constant, non-secret setting is promoted to a Python overlay code
default: the active overlay's ``OverlayConfig`` field (fed by its
``overlay_settings.py``) supplies the value, still DB-overridable. The
effective-settings resolver (:mod:`teatree.config.resolution`) inserts an OVERLAY
CODE DEFAULT tier BETWEEN the DB(global) row tier and the ``UserSettings``
dataclass default, so per promoted key:

    env -> DB(overlay) -> DB(global) -> overlay code default -> dataclass default

The overlay object lives in ``teatree.core`` (domain), ABOVE ``teatree.config``
(platform), so — exactly like :mod:`teatree.mcp.command_catalogue` — the
dependency is INVERTED: ``teatree.core`` registers a provider at overlay-load
time via :func:`register_overlay_code_default_provider`, and this low module
holds only the registration seam plus the promoted-key set.

The default provider fails SAFE to ``{}`` (never raises): config resolution runs
in cold / no-overlay / no-Django contexts where no provider is registered, and
in those the chain must fall straight through to the dataclass default exactly as
before this seam existed. That empty return is also the one-line revert path —
unregister the provider and resolution is byte-identical to pre-#36.
"""

from collections.abc import Callable
from typing import Any

# The ``UserSettings`` fields promoted to an overlay code default (#36). Each is a
# genuinely-constant, non-secret, public skill / regex value already present
# verbatim in the public repo: the active overlay's ``OverlayConfig`` supplies the
# code default, a ``ConfigSetting`` row still overrides it, and with no row the
# code default wins over the dataclass default.
PROMOTED_OVERLAY_CODE_DEFAULT_KEYS: frozenset[str] = frozenset(
    {
        "review_skill",
        "architectural_review_skill",
        "scanning_news_skill",
        "eval_local_skill",
        "backlog_sweep_skill",
        "dogfood_smoke_skill",
        "mr_title_regex",
    }
)

OverlayCodeDefaultProvider = Callable[[str], dict[str, Any]]


def _unregistered_provider(overlay_name: str) -> dict[str, Any]:
    del overlay_name
    return {}


_provider: OverlayCodeDefaultProvider = _unregistered_provider


def register_overlay_code_default_provider(provider: OverlayCodeDefaultProvider) -> None:
    """Inject the overlay-code-default reader (called by ``teatree.core`` at overlay-load time)."""
    global _provider  # noqa: PLW0603 — the single registration seam for the inverted dependency
    _provider = provider


def overlay_code_defaults(overlay_name: str) -> dict[str, Any]:
    """The promoted-key code defaults for *overlay_name* via the registered provider.

    ``{}`` when no overlay is active, no provider is registered, or the provider
    cannot resolve the overlay — the resolver then falls through to the dataclass
    default, matching pre-#36 behaviour exactly.
    """
    if not overlay_name:
        return {}
    return _provider(overlay_name)
