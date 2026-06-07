"""Tests for the overlay-aware backend factory bridge."""

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab.ci import GitLabCIService
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core import backend_factory
from teatree.core.backend_factory import (
    ci_service_from_overlay,
    code_host_for_repo_from_overlay,
    code_host_from_overlay,
    messaging_from_overlay,
    reset_backend_caches,
)
from teatree.core.overlay import OverlayBase, OverlayConfig


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_backend_caches()
    yield
    reset_backend_caches()


class _TokenConfig(OverlayConfig):
    def get_gitlab_token(self) -> str:
        return "gl-test-token"


class _TokenOverlay(OverlayBase):
    config = _TokenConfig()

    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []


class _NoTokenOverlay(OverlayBase):
    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []


def _patch_overlay(overlay_cls):
    return patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={"test": overlay_cls()},
    )


def test_code_host_from_overlay_returns_none_when_no_token() -> None:
    with _patch_overlay(_NoTokenOverlay):
        assert code_host_from_overlay() is None


def test_code_host_from_overlay_returns_gitlab_when_token_present() -> None:
    with _patch_overlay(_TokenOverlay):
        result = code_host_from_overlay()
        assert isinstance(result, GitLabCodeHost)


def test_ci_service_from_overlay_returns_none_when_no_token() -> None:
    with _patch_overlay(_NoTokenOverlay):
        assert ci_service_from_overlay() is None


def test_ci_service_from_overlay_returns_gitlab_when_token_present() -> None:
    with _patch_overlay(_TokenOverlay):
        result = ci_service_from_overlay()
        assert isinstance(result, GitLabCIService)


def test_code_host_from_overlay_returns_none_when_overlay_not_configured() -> None:
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={},
    ):
        assert code_host_from_overlay() is None


def test_ci_service_from_overlay_returns_none_when_overlay_not_configured() -> None:
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={},
    ):
        assert ci_service_from_overlay() is None


def test_messaging_from_overlay_returns_none_when_overlay_not_configured() -> None:
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={},
    ):
        assert messaging_from_overlay() is None


def test_messaging_from_overlay_delegates_to_loader() -> None:
    with (
        _patch_overlay(_NoTokenOverlay),
        patch("teatree.backends.loader.get_messaging", return_value="sentinel") as get_messaging_mock,
    ):
        result = messaging_from_overlay()

    assert result == "sentinel"
    get_messaging_mock.assert_called_once()


def test_reset_backend_caches_clears_all_caches() -> None:
    with _patch_overlay(_TokenOverlay):
        first = code_host_from_overlay()
    reset_backend_caches()
    with _patch_overlay(_NoTokenOverlay):
        second = code_host_from_overlay()
    assert first is not second


def _toml_only_config(overlays: dict) -> object:
    return type("Cfg", (), {"raw": {"overlays": overlays}})()


class TestMessagingFromOverlayTomlFallback:
    """A path-only TOML overlay (no ``class:`` key) still resolves a backend.

    Regression: a wrapper script that sets ``T3_OVERLAY_NAME`` and calls
    ``django.setup()`` would get ``None`` because ``_discover_overlays``
    skips path-only TOML entries — the messaging factory then never
    consulted the TOML fallback that ``iter_overlay_backends`` uses,
    silently routing DMs to the wrong overlay's bot.
    """

    def test_falls_back_to_toml_when_overlay_class_missing(self) -> None:
        cfg = _toml_only_config(
            {
                "private-x": {
                    "path": "~/workspace/private-x",
                    "messaging_backend": "slack",
                    "slack_token_ref": "teatree/private-x/slack",
                    "slack_user_id": "U1",
                },
            },
        )
        seen: list[str] = []

        def fake_read(key: str) -> str:
            seen.append(key)
            return {
                "teatree/private-x/slack-bot": "xoxb-bot-tok",
                "teatree/private-x/slack-app": "xapp-app-tok",
            }.get(key, "")

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=fake_read),
        ):
            backend = messaging_from_overlay(overlay_name="private-x")

        assert isinstance(backend, SlackBotBackend)
        assert "teatree/private-x/slack-bot" in seen
        assert "teatree/private-x/slack-app" in seen

    def test_explicit_overlay_name_wins_over_env_var(self) -> None:
        cfg = _toml_only_config(
            {
                "private-x": {
                    "path": "~/workspace/private-x",
                    "messaging_backend": "slack",
                    "slack_token_ref": "teatree/private-x/slack",
                },
                "teatree": {
                    "messaging_backend": "slack",
                    "slack_token_ref": "teatree/teatree/slack",
                },
            },
        )
        seen: list[str] = []

        def fake_read(key: str) -> str:
            seen.append(key)
            return {
                "teatree/private-x/slack-bot": "xoxb-x-bot",
                "teatree/private-x/slack-app": "xapp-x-app",
                "teatree/teatree/slack-bot": "xoxb-teatree-bot",
                "teatree/teatree/slack-app": "xapp-teatree-app",
            }.get(key, "")

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=fake_read),
            patch.dict(os.environ, {"T3_OVERLAY_NAME": "teatree"}, clear=False),
        ):
            backend = messaging_from_overlay(overlay_name="private-x")

        assert isinstance(backend, SlackBotBackend)
        # Read keys must come from private-x, not teatree.
        assert any(k.startswith("teatree/private-x/") for k in seen)
        assert not any(k.startswith("teatree/teatree/") for k in seen)

    def test_reads_env_var_when_overlay_name_not_passed(self) -> None:
        cfg = _toml_only_config(
            {
                "private-x": {
                    "messaging_backend": "slack",
                    "slack_token_ref": "teatree/private-x/slack",
                },
            },
        )

        def fake_read(key: str) -> str:
            return "xoxb-x-bot" if key == "teatree/private-x/slack-bot" else ""

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=fake_read),
            patch.dict(os.environ, {"T3_OVERLAY_NAME": "private-x"}, clear=False),
        ):
            backend = messaging_from_overlay()

        assert isinstance(backend, SlackBotBackend)

    def test_returns_none_when_named_overlay_absent_from_toml(self) -> None:
        cfg = _toml_only_config({})
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
        ):
            assert messaging_from_overlay(overlay_name="ghost") is None

    def test_caches_separately_per_overlay_name(self) -> None:
        cfg = _toml_only_config(
            {
                "private-x": {"messaging_backend": "slack", "slack_token_ref": "ref-x"},
                "teatree": {"messaging_backend": "slack", "slack_token_ref": "ref-tt"},
            },
        )

        def fake_read(key: str) -> str:
            return {
                "ref-x-bot": "xoxb-x-bot",
                "ref-tt-bot": "xoxb-tt-bot",
            }.get(key, "")

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=fake_read),
        ):
            x = messaging_from_overlay(overlay_name="private-x")
            tt = messaging_from_overlay(overlay_name="teatree")

        assert isinstance(x, SlackBotBackend)
        assert isinstance(tt, SlackBotBackend)
        assert x is not tt


class TestCodeHostFromOverlayTomlFallback:
    def test_falls_back_to_toml_host_when_overlay_class_missing(self) -> None:
        cfg = _toml_only_config(
            {
                "private-x": {
                    "path": "~/workspace/private-x",
                    "github_token_ref": "github/private-x/pat",
                },
            },
        )

        def fake_read(key: str) -> str:
            return "ghp-test" if key == "github/private-x/pat" else ""

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=fake_read),
        ):
            host = code_host_from_overlay(overlay_name="private-x")

        assert isinstance(host, GitHubCodeHost)

    def test_returns_none_when_named_overlay_absent_from_toml(self) -> None:
        cfg = _toml_only_config({})
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
        ):
            assert code_host_from_overlay(overlay_name="ghost") is None


class _BothTokenConfig(OverlayConfig):
    def get_github_token(self) -> str:
        return "gh-test-token"

    def get_gitlab_token(self) -> str:
        return "gl-test-token"


class _BothTokenOverlay(OverlayBase):
    config = _BothTokenConfig()

    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []


_GIT = shutil.which("git") or "git"


def _git_origin(path: Path, origin_url: str) -> str:
    subprocess.run([_GIT, "-C", str(path), "init", "-q"], check=True, capture_output=True)
    subprocess.run([_GIT, "-C", str(path), "remote", "add", "origin", origin_url], check=True, capture_output=True)
    return str(path)


class TestCodeHostForRepoFromOverlay:
    """#2025: the factory resolves the forge from the repo's origin host."""

    def test_resolves_gitlab_for_gitlab_repo_with_both_tokens(self, tmp_path: Path) -> None:
        repo = _git_origin(tmp_path, "git@gitlab.com:group/repo.git")
        with _patch_overlay(_BothTokenOverlay):
            assert isinstance(code_host_for_repo_from_overlay(repo), GitLabCodeHost)

    def test_resolves_github_for_github_repo_with_both_tokens(self, tmp_path: Path) -> None:
        repo = _git_origin(tmp_path, "git@github.com:souliane/teatree.git")
        with _patch_overlay(_BothTokenOverlay):
            assert isinstance(code_host_for_repo_from_overlay(repo), GitHubCodeHost)

    def test_falls_back_to_toml_when_overlay_class_missing(self, tmp_path: Path) -> None:
        repo = _git_origin(tmp_path, "git@github.com:org/repo.git")
        cfg = _toml_only_config(
            {"private-x": {"path": "~/workspace/private-x", "github_token_ref": "github/private-x/pat"}},
        )
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=cfg),
            patch(
                "teatree.utils.secrets.read_pass", side_effect=lambda k: "ghp" if k == "github/private-x/pat" else ""
            ),
        ):
            host = code_host_for_repo_from_overlay(repo, overlay_name="private-x")
        assert isinstance(host, GitHubCodeHost)


class TestActiveOverlayName:
    def test_explicit_name_overrides_env(self) -> None:
        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "env-name"}, clear=False):
            assert backend_factory._active_overlay_name("explicit") == "explicit"

    def test_falls_back_to_env_when_not_provided(self) -> None:
        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "env-name"}, clear=False):
            assert backend_factory._active_overlay_name(None) == "env-name"

    def test_empty_string_when_neither_set(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
        with patch.dict(os.environ, env, clear=True):
            assert backend_factory._active_overlay_name(None) == ""
