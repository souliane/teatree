"""The live route resolves only its named password-store entry."""

import os
from unittest.mock import patch

import pytest

from teatree.llm.credentials import CredentialError, CredentialSpec
from tests.integration.live_control.live_pydantic import LIVE_ROUTE_ENTRY_ENV, resolve_live_route

TEST_ROUTE_ENTRY = "tests/live-pydantic-route"


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
