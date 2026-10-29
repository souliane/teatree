"""The live route resolves only its named password-store entry."""

import pytest

from teatree.llm.credentials import CredentialError, CredentialSpec
from tests.integration.live_control.live_pydantic import LIVE_ROUTE_ENTRY, resolve_live_route


class _Source:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.path: str | None = None

    def lookup(self, spec: CredentialSpec) -> str | None:
        self.path = spec.pass_path
        return self.value


def test_live_route_resolves_the_named_entry_without_an_environment_key() -> None:
    source = _Source("fake-key")
    config = resolve_live_route(source=source)
    assert source.path == LIVE_ROUTE_ENTRY
    assert config.backend.credential_entry == LIVE_ROUTE_ENTRY
    assert config.max_tokens == 512


def test_unresolvable_live_route_names_the_entry() -> None:
    with pytest.raises(CredentialError, match=LIVE_ROUTE_ENTRY):
        resolve_live_route(source=_Source(None))
