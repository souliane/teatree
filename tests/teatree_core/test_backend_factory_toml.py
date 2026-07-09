"""Registry-overlay path in the backend factory — non-entry-point overlays.

Covers ``iter_overlay_backends`` + the ``_backends_from_toml`` helper chain,
which is how teatree picks up overlay configuration from the DB-home
``overlays`` registry (legacy file tier removed) without a registered
``teatree.overlays`` entry point. The ``load_config().raw["overlays"]`` dict
the helpers consume is fed by that registry, so the tests drive it by
injecting the resolved config directly.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.slack.bot import SlackBotBackend
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


class _StubProvider:
    """Backend provider double — drives ``iter_overlay_backends``' collaborator.

    ``hosts`` / ``messaging`` are either a value to return or an exception
    instance to raise, so a single stub covers the credentials-resolve and
    per-backend-error branches.
    """

    def __init__(self, *, hosts: object = (), messaging: object = None):
        self._hosts = hosts
        self._messaging = messaging

    def get_code_hosts(self, overlay: object) -> object:
        if isinstance(self._hosts, Exception):
            raise self._hosts
        return self._hosts

    def get_messaging(self, overlay: object) -> object:
        if isinstance(self._messaging, Exception):
            raise self._messaging
        return self._messaging


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
            patch.object(
                backend_factory,
                "get_backend_provider",
                return_value=_StubProvider(hosts=["HOST"], messaging="MSG"),
            ),
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
            patch.object(
                backend_factory,
                "get_backend_provider",
                return_value=_StubProvider(hosts=ImproperlyConfigured(), messaging=ValueError()),
            ),
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
            patch.object(
                backend_factory,
                "get_backend_provider",
                return_value=_StubProvider(hosts=[], messaging=None),
            ),
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
            return {"ref-bot": "xoxb-bot-tok", "ref-app": "xapp-app-tok"}.get(key, "")

        with patch("teatree.utils.secrets.read_pass", side_effect=fake_read):
            backend = backend_factory._messaging_from_toml(cfg)
        assert isinstance(backend, SlackBotBackend)

    def test_resolves_user_token_ref_from_pass(self) -> None:
        """``user_token_ref`` is honoured by the TOML path-only resolver.

        A wrapper script driving an overlay through the TOML fallback
        (no registered ``teatree.overlays`` entry point) still needs
        reactions to route through the xoxp token, so the TOML resolver
        must read ``user_token_ref`` and thread it into ``SlackBotBackend``.
        """
        cfg = {
            "messaging_backend": "slack",
            "slack_token_ref": "ref",
            "user_token_ref": "slack/user-oauth",
            "slack_user_id": "U1",
        }
        pass_lookups = {"ref-bot": "xoxb-bot-tok", "ref-app": "xapp-app-tok", "slack/user-oauth": "xoxp-tok"}
        with patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookups.get(k, "")):
            backend = backend_factory._messaging_from_toml(cfg)
        assert isinstance(backend, SlackBotBackend)
        assert backend.user_token == "xoxp-tok"

    def test_user_token_empty_when_ref_absent(self) -> None:
        cfg = {"messaging_backend": "slack", "slack_token_ref": "ref"}

        def fake_read(key: str) -> str:
            return {"ref-bot": "xoxb-bot-tok", "ref-app": "xapp-app-tok"}.get(key, "")

        with patch("teatree.utils.secrets.read_pass", side_effect=fake_read):
            backend = backend_factory._messaging_from_toml(cfg)
        assert isinstance(backend, SlackBotBackend)
        assert backend.user_token == ""


class TestLoopAssemblySurvivesMalformedUserToken:
    """A malformed Slack user token must NOT wedge the loop's backend assembly.

    ``iter_overlay_backends`` is the exact construction path ``t3 loop tick``
    runs without ``--overlay``. The #1285 follow-up bug: a Slack-only
    credential typo (an ``xoxb-…`` in the ``xoxp`` user slot) raised
    ``TokenSlotMismatchError`` inside ``SlackBotBackend.__init__`` and
    ``iter_overlay_backends`` demoted Slack to ``None`` — disabling bot DMs
    and all non-Slack work alike on a single credential typo. The fix
    degrades the user token to bot-only so code-host (PR/CI/merge) work and
    bot DMs both keep working.
    """

    def test_iter_overlay_backends_keeps_code_host_and_bot_slack_when_user_token_bad(self) -> None:
        toml_cfg = {
            "messaging_backend": "slack",
            "slack_token_ref": "ref",
            "user_token_ref": "slack/user-oauth",
            "gitlab_token_ref": "ref/gitlab",
            "slack_user_id": "U1",
        }
        pass_lookups = {
            "ref-bot": "xoxb-real-bot",
            "ref-app": "xapp-real-app",
            "slack/user-oauth": "xoxb-mistakenly-pasted-into-user-slot",
            "ref/gitlab": "gl-tok",
        }
        cfg = _config_with({"acme": toml_cfg})
        with (
            patch.object(backend_factory, "get_all_overlays", return_value={}),
            patch.object(backend_factory, "_resolved_identities", return_value=()),
            patch("teatree.config.load_config", return_value=cfg),
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookups.get(k, "")),
        ):
            out = backend_factory.iter_overlay_backends()

        acme = next(b for b in out if b.name == "acme")
        # Non-Slack work (the code host) is fully present — the loop can
        # still merge, run CI, and sweep PRs.
        assert [type(h).__name__ for h in acme.hosts] == [GitLabCodeHost.__name__]
        # Slack degraded to bot-only rather than vanishing entirely.
        assert isinstance(acme.messaging, SlackBotBackend)
        assert acme.messaging.user_token == ""


class TestFindExternalDb:
    def test_returns_none_when_path_missing(self) -> None:
        assert backend_factory._find_external_db("foo", {}) is None

    def test_returns_db_path_when_sqlite_present(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        assert backend_factory._find_external_db("foo", {"path": str(tmp_path)}) == db
