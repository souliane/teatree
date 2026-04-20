from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.overlay_loader import reset_overlay_cache
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestOverlayConfig:
    def test_lists_all_config_keys(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("overlay", "config")

        assert "mr_close_ticket:" in result
        assert "require_ticket:" in result

    def test_returns_single_key_value(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("overlay", "config", "--key", "mr_close_ticket")

        assert result == "False"

    def test_returns_error_for_unknown_key(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("overlay", "config", "--key", "nonexistent")

        assert "Unknown config key" in result
