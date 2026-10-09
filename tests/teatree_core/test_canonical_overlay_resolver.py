"""Stored overlay names are exact entry-point names after migration 0125."""

from unittest.mock import patch

from teatree.core.overlay_name_resolution import resolve_overlay_name


def test_registered_name_is_dispatchable() -> None:
    with patch("teatree.core.overlay_loader.OverlayConfigResolver.all_names", return_value=["t3-teatree"]):
        assert resolve_overlay_name("t3-teatree") == "t3-teatree"


def test_short_name_is_not_dispatchable() -> None:
    with patch("teatree.core.overlay_loader.OverlayConfigResolver.all_names", return_value=["t3-teatree"]):
        assert resolve_overlay_name("teatree") is None
