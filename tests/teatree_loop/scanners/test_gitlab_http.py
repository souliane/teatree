"""The scanner GitLab client binds the owning overlay's host and token."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.loop.scanners.gitlab_http import MissingGitLabTokenError, gitlab_api_for


def _resolution(state: ForgeTokenState = ForgeTokenState.TOKEN) -> ForgeTokenResolution:
    token = "owned-token" if state is ForgeTokenState.TOKEN else ""
    return ForgeTokenResolution("gitlab_token", "owner", state, token=token)


def test_live_overlay_routes_host_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    get_overlay = Mock(return_value=SimpleNamespace(config=SimpleNamespace(gitlab_url="https://owned.example/api/v4")))
    monkeypatch.setattr("teatree.core.overlay_loader.get_overlay", get_overlay)
    api = gitlab_api_for(_resolution())
    assert (api.base_url, api.token) == ("https://owned.example/api/v4", "owned-token")
    get_overlay.assert_called_once_with("owner")


def test_toml_only_overlay_routes_host_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> object:
        raise ImproperlyConfigured

    monkeypatch.setattr("teatree.core.overlay_loader.get_overlay", missing)
    monkeypatch.setattr(
        "teatree.loop.scanners.gitlab_http.load_config",
        lambda: SimpleNamespace(raw={"overlays": {"owner": {"gitlab_url": "https://toml.example/"}}}),
    )
    api = gitlab_api_for(_resolution())
    assert (api.base_url, api.token) == ("https://toml.example/api/v4", "owned-token")


def test_unresolved_token_never_constructs_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    monkeypatch.setattr("teatree.loop.scanners.gitlab_http.GitLabAPI", client)
    with pytest.raises(MissingGitLabTokenError):
        gitlab_api_for(_resolution(ForgeTokenState.UNREADABLE))
    client.assert_not_called()


def test_empty_token_never_constructs_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    monkeypatch.setattr("teatree.loop.scanners.gitlab_http.GitLabAPI", client)
    resolution = ForgeTokenResolution("gitlab_token", "owner", ForgeTokenState.TOKEN, token="")
    with pytest.raises(MissingGitLabTokenError):
        gitlab_api_for(resolution)
    client.assert_not_called()
