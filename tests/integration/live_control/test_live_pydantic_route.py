"""The live route resolves only its named password-store entry."""

import os
from unittest.mock import patch

import pytest

from teatree.llm.credentials import CredentialError, CredentialSpec
from tests.integration.live_control.live_pydantic import (
    LIVE_ROUTE_DEFAULT_MODEL,
    LIVE_ROUTE_ENTRY_ENV,
    LIVE_ROUTE_MODEL_ENV,
    resolve_live_route,
)

TEST_ROUTE_ENTRY = "tests/live-pydantic-route"
RETIRED_SLUG = "qwen/qwen3-235b-a22b-instruct-2507"


class _Source:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.path: str | None = None

    def lookup(self, spec: CredentialSpec) -> str | None:
        self.path = spec.pass_path
        return self.value


def test_live_route_resolves_the_named_entry_without_an_environment_key() -> None:
    source = _Source("fake-key")
    with patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: TEST_ROUTE_ENTRY}):
        config = resolve_live_route(source=source)
    assert source.path == TEST_ROUTE_ENTRY
    assert config.backend.credential_entry == TEST_ROUTE_ENTRY
    assert config.max_tokens == 512


def test_unresolvable_live_route_names_the_entry() -> None:
    with (
        patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: TEST_ROUTE_ENTRY}),
        pytest.raises(CredentialError, match=TEST_ROUTE_ENTRY),
    ):
        resolve_live_route(source=_Source(None))


def test_live_route_needs_an_explicit_entry_before_reading_the_store() -> None:
    source = _Source("fake-key")
    with patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: ""}), pytest.raises(CredentialError, match=LIVE_ROUTE_ENTRY_ENV):
        resolve_live_route(source=source)
    assert source.path is None


def test_live_route_defaults_to_a_model_the_provider_still_serves() -> None:
    with patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: TEST_ROUTE_ENTRY}):
        os.environ.pop(LIVE_ROUTE_MODEL_ENV, None)
        config = resolve_live_route(source=_Source("fake-key"))
    assert config.backend.model == LIVE_ROUTE_DEFAULT_MODEL == "openai/gpt-oss-120b"
    assert config.backend.model != RETIRED_SLUG


def test_live_route_model_follows_the_environment_override() -> None:
    with patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: TEST_ROUTE_ENTRY, LIVE_ROUTE_MODEL_ENV: " vendor/other-model "}):
        config = resolve_live_route(source=_Source("fake-key"))
    assert config.backend.model == "vendor/other-model"


def test_blank_model_override_falls_back_to_the_default() -> None:
    with patch.dict(os.environ, {LIVE_ROUTE_ENTRY_ENV: TEST_ROUTE_ENTRY, LIVE_ROUTE_MODEL_ENV: "  "}):
        config = resolve_live_route(source=_Source("fake-key"))
    assert config.backend.model == LIVE_ROUTE_DEFAULT_MODEL
