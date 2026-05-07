from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

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


class TestOverlayInfo:
    def test_returns_overlay_class_path(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("overlay", "info")

        assert "CommandOverlay" in result
        assert "tests.teatree_core.conftest" in result


class TestOverlayContractCheck:
    def test_raises_when_compose_is_empty(self) -> None:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(CommandError, match="--compose is required"),
        ):
            call_command("overlay", "contract-check")

    def test_raises_when_compose_file_missing(self, tmp_path: Path) -> None:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(CommandError, match="not found"),
        ):
            call_command("overlay", "contract-check", "--compose", str(tmp_path / "missing.yml"))

    def test_returns_ok_when_no_violations(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  app:\n    image: ${MISSING_KEY:-default}\n")

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("overlay", "contract-check", "--compose", str(compose))

        assert "ok" in result

    def test_exits_when_violations_found(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  app:\n    image: ${UNKNOWN_VAR}\n")

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            call_command("overlay", "contract-check", "--compose", str(compose))

        captured = capsys.readouterr()
        assert "UNKNOWN_VAR" in captured.err
        assert "no declared producer" in captured.err

    def test_allowed_keys_are_skipped(self, tmp_path: Path) -> None:
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  app:\n    image: ${ALLOWED_VAR}\n")

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command(
                "overlay",
                "contract-check",
                "--compose",
                str(compose),
                "--allow",
                "ALLOWED_VAR",
            )

        assert "ok" in result
