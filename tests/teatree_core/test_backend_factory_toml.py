"""TOML-overlay path in the backend factory — non-entry-point overlays.

Covers ``iter_overlay_backends`` + the ``_backends_from_toml`` helper chain,
which is how teatree picks up overlay configuration written directly in
``~/.teatree.toml`` without a registered ``teatree.overlays`` entry point.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.slack_bot import SlackBotBackend
from teatree.core import backend_factory


@dataclass
class _Cfg:
    gitlab_url: str = "https://gitlab.com"
    ready_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    auto_start_assigned_issues: bool = False
    max_concurrent_auto_starts: int = 1
    stale_threshold_days: int = 3

    def get_gitlab_token(self) -> str:
        return "tok"


@dataclass
class _Overlay:
    name: str
    config: _Cfg


def _config_with(overlays: dict[str, Any]) -> object:
    return type("Cfg", (), {"raw": {"overlays": overlays}})()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    backend_factory.reset_backend_caches()
    yield
    backend_factory.reset_backend_caches()


class TestIterOverlayBackendsEntryPoints:
    def test_collects_python_overlays_with_their_backends(self) -> None:
        overlay = _Overlay("foo", _Cfg(ready_labels=("ready",), exclude_labels=("wip",)))
        with (
            patch.object(backend_factory, "get_all_overlays", return_value={"foo": overlay}),
            patch.object(backend_factory, "get_code_hosts", return_value=["HOST"]),
            patch.object(backend_factory, "get_messaging", return_value="MSG"),
            patch.object(backend_factory, "_backends_from_toml", return_value=[]),
        ):
            out = backend_factory.iter_overlay_backends()
        assert len(out) == 1
        assert out[0].name == "foo"
        assert out[0].hosts == ("HOST",)
        assert out[0].host == "HOST"
        assert out[0].messaging == "MSG"
        assert out[0].ready_labels == ("ready",)
        assert out[0].exclude_labels == ("wip",)
        assert out[0].overlay is overlay

    def test_swallows_credential_errors_per_backend(self) -> None:
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        overlay = _Overlay("foo", _Cfg())
        with (
            patch.object(backend_factory, "get_all_overlays", return_value={"foo": overlay}),
            patch.object(backend_factory, "get_code_hosts", side_effect=ImproperlyConfigured),
            patch.object(backend_factory, "get_messaging", side_effect=ValueError),
            patch.object(backend_factory, "_backends_from_toml", return_value=[]),
        ):
            out = backend_factory.iter_overlay_backends()
        assert out[0].hosts == ()
        assert out[0].host is None
        assert out[0].messaging is None

    def test_appends_toml_only_overlays_to_python_overlays(self) -> None:
        py_overlay = _Overlay("py", _Cfg())
        toml_backend = backend_factory.OverlayBackends(name="toml-only", hosts=(), messaging=None, ready_labels=())
        with (
            patch.object(backend_factory, "get_all_overlays", return_value={"py": py_overlay}),
            patch.object(backend_factory, "get_code_hosts", return_value=[]),
            patch.object(backend_factory, "get_messaging", return_value=None),
            patch.object(backend_factory, "_backends_from_toml", return_value=[toml_backend]) as mock_toml,
        ):
            out = backend_factory.iter_overlay_backends()
        mock_toml.assert_called_once_with({"py"}, ())
        assert [b.name for b in out] == ["py", "toml-only"]


class TestBackendsFromToml:
    def test_skips_overlays_already_found_via_entry_point(self) -> None:
        cfg = _config_with({"foo": {"gitlab_token_ref": "x"}})
        with patch("teatree.config.load_config", return_value=cfg):
            assert backend_factory._backends_from_toml({"foo"}) == []

    def test_skips_non_dict_overlay_entries(self) -> None:
        cfg = _config_with({"foo": "not-a-dict"})
        with patch("teatree.config.load_config", return_value=cfg):
            assert backend_factory._backends_from_toml(set()) == []

    def test_drops_overlay_with_no_host_messaging_or_db(self) -> None:
        cfg = _config_with({"foo": {}})
        with patch("teatree.config.load_config", return_value=cfg):
            assert backend_factory._backends_from_toml(set()) == []

    def test_includes_overlay_with_only_external_db(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        cfg = _config_with({"foo": {"path": str(tmp_path), "ready_labels": ["ok"], "exclude_labels": ["x"]}})
        with patch("teatree.config.load_config", return_value=cfg):
            out = backend_factory._backends_from_toml(set())
        assert len(out) == 1
        assert out[0].external_db == db
        assert out[0].ready_labels == ("ok",)
        assert out[0].exclude_labels == ("x",)


class TestHostFromToml:
    def test_returns_gitlab_host_when_token_available(self) -> None:
        cfg = {"gitlab_token_ref": "ref/gitlab", "gitlab_url": "https://gl.example"}
        with patch("teatree.utils.secrets.read_pass", return_value="tok"):
            host = backend_factory._host_from_toml(cfg)
        assert isinstance(host, GitLabCodeHost)

    def test_returns_github_host_when_only_github_token_set(self) -> None:
        cfg = {"github_token_ref": "ref/github"}

        def fake_read(key: str) -> str:
            return "tok" if key == "ref/github" else ""

        with patch("teatree.utils.secrets.read_pass", side_effect=fake_read):
            host = backend_factory._host_from_toml(cfg)
        assert isinstance(host, GitHubCodeHost)

    def test_returns_none_when_token_ref_set_but_pass_empty(self) -> None:
        cfg = {"gitlab_token_ref": "ref"}
        with patch("teatree.utils.secrets.read_pass", return_value=""):
            assert backend_factory._host_from_toml(cfg) is None

    def test_returns_none_when_no_token_refs_in_config(self) -> None:
        assert backend_factory._host_from_toml({}) is None


class TestMessagingFromToml:
    def test_returns_none_when_backend_is_not_slack(self) -> None:
        assert backend_factory._messaging_from_toml({"messaging_backend": "teams"}) is None

    def test_returns_none_when_token_ref_missing(self) -> None:
        assert backend_factory._messaging_from_toml({"messaging_backend": "slack"}) is None

    def test_returns_none_when_bot_token_not_in_pass(self) -> None:
        cfg = {"messaging_backend": "slack", "slack_token_ref": "ref"}
        with patch("teatree.utils.secrets.read_pass", return_value=""):
            assert backend_factory._messaging_from_toml(cfg) is None

    def test_returns_slack_backend_when_credentials_present(self) -> None:
        cfg = {"messaging_backend": "slack", "slack_token_ref": "ref", "slack_user_id": "U1"}

        def fake_read(key: str) -> str:
            return {"ref-bot": "bot-tok", "ref-app": "app-tok"}.get(key, "")

        with patch("teatree.utils.secrets.read_pass", side_effect=fake_read):
            backend = backend_factory._messaging_from_toml(cfg)
        assert isinstance(backend, SlackBotBackend)


class TestFindExternalDb:
    def test_returns_none_when_path_missing(self) -> None:
        assert backend_factory._find_external_db("foo", {}) is None

    def test_returns_db_path_when_sqlite_present(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        assert backend_factory._find_external_db("foo", {"path": str(tmp_path)}) == db
