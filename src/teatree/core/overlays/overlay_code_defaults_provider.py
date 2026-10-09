"""Core-side provider for the overlay-code-default config seam (#36).

Registers the reader the effective-settings resolver consults for promoted
constants (`teatree.config.overlay_code_defaults`). The reader needs
`get_overlay`, which lives in `overlay_loader`; that module calls
:func:`build_and_register` with its own `get_overlay` at import time, so this
module depends on neither `overlay_loader` nor the resolver — the dependency
points one way and there is no import cycle.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.config.overlay_code_defaults import (
    PROMOTED_OVERLAY_CODE_DEFAULT_KEYS,
    register_overlay_code_default_provider,
)

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

OverlayGetter = Callable[[str], "OverlayBase"]


def build_and_register(get_overlay: OverlayGetter) -> None:
    """Register a provider that reads *get_overlay*'s ``OverlayConfig`` for promoted keys.

    Fails SAFE to ``{}`` for any unresolvable overlay (unknown name, path-only
    entry, pre-Django) so the resolver falls through to the dataclass default
    exactly as before the seam existed. The fall-through is logged at WARNING: an empty
    return here also hands the read to the cold tier, which fails safe again, so without
    a line here a gate reading a promoted declaration goes inert in complete silence.
    """

    def _provider(overlay_name: str) -> dict[str, str]:
        try:
            config = get_overlay(overlay_name).config
            return {key: getattr(config, key) for key in PROMOTED_OVERLAY_CODE_DEFAULT_KEYS}
        except Exception:
            logger.warning(
                "overlay %r: could not read promoted code defaults off its OverlayConfig",
                overlay_name,
                exc_info=True,
            )
            return {}

    register_overlay_code_default_provider(_provider)
