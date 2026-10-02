# test-path: cross-cutting

from collections.abc import Callable
from unittest.mock import MagicMock, patch

from teatree.config.credential_pass_key import PassKeyResolution, PassKeySource
from teatree.core.overlays.forge_credential_provider import build_and_register
from teatree.forge_credentials import ForgeTokenState, resolve_overlay_token, resolve_repo_token, resolve_slug_token


def _overlay(*, route: str, source: PassKeySource = PassKeySource.OVERLAY_DB) -> MagicMock:
    overlay = MagicMock()
    overlay.config.resolve_pass_key.return_value = PassKeyResolution("github_token_pass_key", route, source)
    return overlay


def _register(
    overlay: MagicMock,
    *,
    infer: Callable[[str], str | None] = lambda _target: "owner",
    owned_repos: Callable[[str], dict[str, list[str]]] = lambda _name: {},
) -> None:
    build_and_register(
        get_overlay=lambda _name=None: overlay,
        get_overlay_for_repo=lambda _repo: overlay,
        infer_overlay_for_url=infer,
        all_overlay_names=lambda: ["owner"],
        owned_repos=owned_repos,
    )


def test_overlay_token_preserves_token_unset_and_unreadable() -> None:
    overlay = _overlay(route="owner/github")
    _register(overlay)
    with patch("teatree.core.overlays.forge_credential_provider.read_pass", return_value="routed-token"):
        token = resolve_overlay_token(overlay, credential="github_token", overlay_name="owner")

    overlay.config.resolve_pass_key.return_value = PassKeyResolution("github_token_pass_key", "", PassKeySource.UNSET)
    unset = resolve_overlay_token(overlay, credential="github_token", overlay_name="owner")
    overlay.config.resolve_pass_key.return_value = PassKeyResolution(
        "github_token_pass_key", "", PassKeySource.UNREADABLE
    )
    unreadable = resolve_overlay_token(overlay, credential="github_token", overlay_name="owner")

    assert (token.state, token.token, token.overlay_name) == (ForgeTokenState.TOKEN, "routed-token", "owner")
    assert unset.state is ForgeTokenState.UNSET
    assert unreadable.state is ForgeTokenState.UNREADABLE


def test_repo_token_uses_owning_overlay_and_ignores_hostile_ambient(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "hostile-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "hostile-github")
    monkeypatch.setenv("TEATREE_GH_TOKEN", "bootstrap-only")
    overlay = _overlay(route="owner/github")
    infer = MagicMock(return_value="owner")
    _register(overlay, infer=infer)
    with (
        patch(
            "teatree.core.overlays.forge_credential_provider.git.remote_url",
            return_value="git@github.com:owner/repo.git",
        ),
        patch("teatree.core.overlays.forge_credential_provider.read_pass", return_value="db-routed"),
    ):
        resolution = resolve_repo_token("/repo", credential="github_token")

    infer.assert_called_once_with("git@github.com:owner/repo.git")
    assert resolution.state is ForgeTokenState.TOKEN
    assert resolution.token == "db-routed"


def test_routed_missing_secret_is_unset_and_store_failure_is_unreadable() -> None:
    overlay = _overlay(route="owner/github")
    _register(overlay)
    with patch("teatree.core.overlays.forge_credential_provider.read_pass", return_value=""):
        missing = resolve_overlay_token(overlay, credential="github_token", overlay_name="owner")
    with patch(
        "teatree.core.overlays.forge_credential_provider.read_pass",
        side_effect=RuntimeError("gpg failed"),
    ):
        failed = resolve_overlay_token(overlay, credential="github_token", overlay_name="owner")

    assert missing.state is ForgeTokenState.UNSET
    assert failed.state is ForgeTokenState.UNREADABLE
    assert "gpg failed" in failed.detail


def test_slug_token_uses_unique_owner_and_preserves_unreadable_route(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "hostile-gh")
    overlay = _overlay(route="", source=PassKeySource.UNREADABLE)
    _register(
        overlay,
        infer=lambda _target: None,
        owned_repos=lambda _name: {"github.com": ["acme"]},
    )

    resolution = resolve_slug_token("acme/widget", forge="github", credential="github_token")

    assert resolution.overlay_name == "owner"
    assert resolution.state is ForgeTokenState.UNREADABLE
    assert resolution.token == ""
