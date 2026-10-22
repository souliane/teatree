"""Tests for teatree.core.overlay_name_resolution — ambient + inverse overlay lookup."""

from functools import lru_cache
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.core import overlay_loader, overlay_name_resolution
from teatree.core.overlay_loader import get_all_overlays
from teatree.core.overlay_name_resolution import cwd_overlay_name, overlay_name_of


def test_overlay_name_of_returns_the_registered_name() -> None:
    overlays = get_all_overlays()
    teatree = overlays["t3-teatree"]
    assert overlay_name_of(teatree) == "t3-teatree"


def test_overlay_name_of_returns_empty_for_none_or_unregistered() -> None:
    assert overlay_name_of(None) == ""
    stranger = SimpleNamespace()  # not any registered overlay instance
    assert overlay_name_of(stranger) == ""


def test_cwd_overlay_name_returns_the_discovered_registered_name() -> None:
    overlays = get_all_overlays()
    entry = SimpleNamespace(name="t3-teatree")
    with patch("teatree.config.discover_active_overlay", return_value=entry):
        assert cwd_overlay_name(overlays) == "t3-teatree"


def test_cwd_overlay_name_is_none_when_discovery_finds_an_unregistered_name() -> None:
    overlays = get_all_overlays()
    entry = SimpleNamespace(name="not-a-registered-overlay")
    with patch("teatree.config.discover_active_overlay", return_value=entry):
        assert cwd_overlay_name(overlays) is None


def test_cwd_overlay_name_is_none_when_discovery_raises() -> None:
    overlays = get_all_overlays()
    with patch("teatree.config.discover_active_overlay", side_effect=RuntimeError("boom")):
        assert cwd_overlay_name(overlays) is None


@pytest.mark.parametrize(
    ("spelling", "names", "expected"),
    [
        ("acme", ("t3-acme",), "t3-acme"),
        ("acme", ("acme", "t3-acme"), "acme"),
        ("t3-acme", ("acme", "t3-acme"), "t3-acme"),
        ("t3-ghost", ("ghost",), "ghost"),
        ("ghost", ("t3-acme",), None),
        ("", ("t3-acme",), None),
    ],
)
def test_registered_overlay_name_qualifies_up_to_the_registered_spelling(
    spelling: str, names: tuple[str, ...], expected: str | None
) -> None:
    overlay = get_all_overlays()["t3-teatree"]
    registry = dict.fromkeys(names, overlay)
    with patch.object(overlay_loader, "_discover_overlays", lru_cache(maxsize=1)(lambda: registry)):
        assert overlay_name_resolution.registered_overlay_name(spelling) == expected
