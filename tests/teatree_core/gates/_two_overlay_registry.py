"""A second registered overlay beside ``t3-teatree``, so the ambiguity is reachable on any install.

The multi-overlay privacy tests used to skip on a single-overlay install, which is
exactly the CI shape — so the regressions they pin were never exercised there.
"""

import functools
from unittest.mock import patch

from django.test import TestCase

import teatree.config
from teatree.config.settings import OverlayEntry
from teatree.core import overlay_loader
from teatree.core.overlay import OverlayBase

SIBLING_OVERLAY = "zz-sibling"


class _SiblingOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: object) -> list:
        del worktree
        return []


def register_a_sibling_overlay(case: TestCase) -> None:
    """Add :data:`SIBLING_OVERLAY` to both views of the registry for the test's duration.

    The config-side list matters too: with one installed entry it names that overlay
    the ambient one, and ``get_overlay`` never reaches the ambiguity.
    """
    registry = {**overlay_loader._discover_overlays(), SIBLING_OVERLAY: _SiblingOverlay()}
    installed = [*teatree.config.discover_overlays(), OverlayEntry(name=SIBLING_OVERLAY, overlay_class="")]
    for patcher in (
        patch.object(overlay_loader, "_discover_overlays", functools.lru_cache(maxsize=1)(lambda: registry)),
        patch.object(teatree.config, "discover_overlays", lambda: installed),
    ):
        patcher.start()
        case.addCleanup(patcher.stop)
