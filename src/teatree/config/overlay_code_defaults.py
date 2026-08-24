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

That fall-through is right for a value nobody enforces and WRONG for a value a
gate reads. Registration happens at ``teatree.core.overlay_loader`` import time,
which a PreToolUse hook never reaches, so for every Bash gate the tier was not
merely empty but structurally absent: a rule declared in ``overlay_settings.py``
resolved to its shipped default at the one seam that enforces it, and the gate
sat inert while the rule it carries was being broken.
:func:`cold_overlay_code_defaults` closes that by reading the SAME declaration
directly — the overlay's settings module is plain constants and imports
Django-free — so the declaration is authoritative on both surfaces and there is
no second place to state it.
"""

import logging
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any

logger = logging.getLogger(__name__)

#: The module every overlay states its constants in, sitting beside the module its
#: ``teatree.overlays`` entry point names. ``OverlayConfig._load_settings`` reads it
#: warm; the cold read below reads the same file through the same convention.
OVERLAY_SETTINGS_MODULE_LEAF = "overlay_settings"

# The ``UserSettings`` fields promoted to an overlay code default (#36). Each is a
# genuinely-constant, non-secret, public skill / regex value already present
# verbatim in the public repo: the active overlay's ``OverlayConfig`` supplies the
# code default, a ``ConfigSetting`` row still overrides it, and with no row the
# code default wins over the dataclass default.
PROMOTED_OVERLAY_CODE_DEFAULT_KEYS: frozenset[str] = frozenset(
    {
        "review_skill",
        "review_skill_alternates",
        "architectural_review_skill",
        "scanning_news_skill",
        "eval_local_skill",
        "backlog_sweep_skill",
        "dogfood_smoke_skill",
        "mr_title_regex",
        # The repos that are ONE branch wide right now. Promoted because the
        # declaration lives in the overlay's ``overlay_settings.py`` while every
        # consumer (the Bash gate, the provisioner, branch classification) reads
        # ``get_effective_settings``: without this tier the two surfaces diverge
        # and the gate reads ``[]`` while the rule is declared.
        "single_branch_repos",
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
    """The promoted-key code defaults for *overlay_name*, warm provider first.

    The registered provider stays authoritative wherever it exists — it reads the
    fully-constructed ``OverlayConfig``, including a ``[overlays.<name>]`` registry
    override the raw settings module cannot see. Only when it yields nothing (no
    provider registered, or an overlay it cannot resolve) does the cold read below
    answer, so a gate running outside Django sees the declaration instead of the
    shipped default. ``{}`` when no overlay is active or neither path resolves —
    the resolver then falls through to the shipped default as it did pre-#36.
    """
    if not overlay_name:
        return {}
    return _provider(overlay_name) or cold_overlay_code_defaults(overlay_name)


def cold_overlay_code_defaults(overlay_name: str) -> dict[str, Any]:
    """The promoted keys *overlay_name*'s settings module declares, read Django-free.

    Only the keys the module actually states are returned. An undeclared key is
    ABSENT rather than filled with a mirror of the shipped default, so this read can
    never introduce a value the overlay did not state — it can only carry one it did.

    Fails safe to ``{}`` on everything: an overlay with no entry point, a settings
    module that is missing or raises on import, an unreadable entry-point table. The
    empty return is right and the SILENCE is not: this is the last tier that can carry a
    declaration a gate enforces, so a settings module that stopped importing takes the
    rule down to its shipped default with nothing anywhere saying so. Logged at WARNING
    with the overlay and the module path, never raised.
    """
    module_path = ""
    try:
        module_path = _overlay_settings_module(overlay_name)
        if not module_path:
            return {}
        module = import_module(module_path)
    except Exception:
        logger.warning(
            "overlay %r: cold read of %r failed; promoted code defaults fall through to the shipped values",
            overlay_name,
            module_path or "<unresolved entry point>",
            exc_info=True,
        )
        return {}
    return {
        key: getattr(module, key.upper()) for key in PROMOTED_OVERLAY_CODE_DEFAULT_KEYS if hasattr(module, key.upper())
    }


def _overlay_settings_module(overlay_name: str) -> str:
    """The dotted path of *overlay_name*'s settings module, or ``""`` when unresolvable.

    Derived from the overlay's ``teatree.overlays`` entry point rather than from the
    ``OverlayConfig`` constructor argument that names it warm: reaching that argument
    means importing (and instantiating) the overlay class, which pulls in Django and
    is exactly what a cold hook cannot do.
    """
    for entry_point in entry_points(group="teatree.overlays"):
        if entry_point.name != overlay_name:
            continue
        package = entry_point.value.partition(":")[0].rpartition(".")[0]
        return f"{package}.{OVERLAY_SETTINGS_MODULE_LEAF}" if package else ""
    return ""
