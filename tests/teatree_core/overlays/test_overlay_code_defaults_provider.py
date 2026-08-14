# test-path: mirror
"""Core-side provider for the overlay-code-default seam (#36).

``build_and_register`` wires a reader that pulls the promoted keys off an
overlay's ``OverlayConfig`` via an injected ``get_overlay``, and fails safe to
``{}`` when the overlay cannot be resolved. Covered here without the real
registry by injecting a stub getter and reading back through the config seam.
"""

import logging
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

import teatree.config.overlay_code_defaults as seam
import teatree.core.overlays.overlay_code_defaults_provider as provider
from teatree.config.overlay_code_defaults import (
    PROMOTED_OVERLAY_CODE_DEFAULT_KEYS,
    overlay_code_defaults,
    register_overlay_code_default_provider,
)
from teatree.core.overlay import OverlayConfig
from teatree.core.overlays.overlay_code_defaults_provider import build_and_register


def test_registered_provider_reads_promoted_keys_off_the_config() -> None:
    original = seam._provider
    config = OverlayConfig()
    config.review_skill = "stub-skill"
    try:
        build_and_register(lambda name: SimpleNamespace(config=config))
        resolved = overlay_code_defaults("any-overlay")
    finally:
        register_overlay_code_default_provider(original)
    assert set(resolved) == set(PROMOTED_OVERLAY_CODE_DEFAULT_KEYS)
    assert resolved["review_skill"] == "stub-skill"
    assert resolved["scanning_news_skill"] == "scanning-news"


def test_provider_fails_safe_when_overlay_unresolvable() -> None:
    original = seam._provider

    def _raises(name: str) -> SimpleNamespace:
        raise ImproperlyConfigured(name)

    try:
        build_and_register(_raises)
        assert overlay_code_defaults("missing-overlay") == {}
    finally:
        register_overlay_code_default_provider(original)


def test_an_unresolvable_overlay_names_itself_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    # The empty return hands the read straight to the cold tier, which fails safe again —
    # so a promoted declaration a gate enforces can go inert with nothing said anywhere.
    original = seam._provider

    def _raises(name: str) -> SimpleNamespace:
        raise ImproperlyConfigured(name)

    try:
        build_and_register(_raises)
        with caplog.at_level(logging.WARNING, logger=provider.__name__):
            assert overlay_code_defaults("broken-overlay") == {}
    finally:
        register_overlay_code_default_provider(original)
    assert "broken-overlay" in caplog.text


def test_single_branch_repos_declared_in_overlay_settings_reaches_the_tier() -> None:
    original = seam._provider
    config = OverlayConfig()
    # The shape ``OverlayConfig._load_settings`` produces from a
    # ``SINGLE_BRANCH_REPOS`` constant in the overlay's ``overlay_settings.py``.
    config.single_branch_repos = ["group/widget-core=chore/fork-bootstrap"]
    try:
        build_and_register(lambda name: SimpleNamespace(config=config))
        resolved = overlay_code_defaults("declaring-overlay")
    finally:
        register_overlay_code_default_provider(original)
    assert resolved["single_branch_repos"] == ["group/widget-core=chore/fork-bootstrap"]


def test_overlay_declaring_nothing_keeps_every_other_promoted_key() -> None:
    original = seam._provider
    config = OverlayConfig()
    try:
        build_and_register(lambda name: SimpleNamespace(config=config))
        resolved = overlay_code_defaults("silent-overlay")
    finally:
        register_overlay_code_default_provider(original)
    assert set(resolved) == set(PROMOTED_OVERLAY_CODE_DEFAULT_KEYS)
    assert resolved["single_branch_repos"] == []
    assert resolved["scanning_news_skill"] == "scanning-news"
