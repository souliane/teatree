"""A dispatched agent's own ``gh`` and ``git`` authenticate through its overlay's routed token.

The long-running containers hold no ambient GitHub token, so an agent that shells out to
``gh`` would otherwise run unauthenticated. The route is the only source: an unset or
unreadable one adds nothing rather than guessing.
"""

import pytest

from teatree import forge_credentials
from teatree.agents._runner_env import with_routed_github_token
from teatree.forge_credentials import ForgeCredentialRequest, ForgeTokenResolution, ForgeTokenState


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch) -> list[ForgeCredentialRequest]:
    seen: list[ForgeCredentialRequest] = []

    def provider(request: ForgeCredentialRequest) -> ForgeTokenResolution:
        seen.append(request)
        if request.overlay_name == "acme":
            return ForgeTokenResolution(request.credential, "acme", ForgeTokenState.TOKEN, token="ghp-routed")
        return ForgeTokenResolution(request.credential, request.overlay_name, ForgeTokenState.UNSET)

    monkeypatch.setattr(forge_credentials, "_provider", provider)
    return seen


def test_the_overlays_routed_token_reaches_the_agent(routed: list[ForgeCredentialRequest]) -> None:
    env = with_routed_github_token({"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"}, overlay="acme")

    assert env == {"PYTEST_XDIST_AUTO_NUM_WORKERS": "2", "GH_TOKEN": "ghp-routed"}
    assert [(r.target, r.credential) for r in routed] == [("acme", "github_token")]


def test_an_inherited_environment_gains_only_the_token(routed: list[ForgeCredentialRequest]) -> None:
    _ = routed
    assert with_routed_github_token(None, overlay="acme") == {"GH_TOKEN": "ghp-routed"}


def test_an_unset_route_leaves_the_environment_alone(routed: list[ForgeCredentialRequest]) -> None:
    _ = routed
    assert with_routed_github_token(None, overlay="other") is None
    assert with_routed_github_token({"A": "1"}, overlay="other") == {"A": "1"}


def test_an_unreadable_route_leaves_the_environment_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(request: ForgeCredentialRequest) -> ForgeTokenResolution:
        raise RuntimeError(request.credential)

    monkeypatch.setattr(forge_credentials, "_provider", broken)

    assert with_routed_github_token({"A": "1"}, overlay="acme") == {"A": "1"}
