"""Tests for the read-only SharePoint/OneDrive rclone backend (#3084).

The rclone subprocess is the only mock: every test patches
``teatree.backends.sharepoint.run_checked`` (the ``subprocess`` chokepoint) so no
real ``rclone`` / network is touched. They pin the command shape, the
encrypted-config env, the read-only enforcement, and the ``?id=`` deep-link.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.backends.sharepoint import SharePointClient
from teatree.types import SharePointRemoteSpec
from teatree.utils.run import CommandFailedError


def _ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _failed(cmd: list[str], rc: int = 3) -> CommandFailedError:
    # rclone maps a Graph 403 to a non-zero exit; rc value is immaterial here.
    return CommandFailedError(cmd, rc, "", "403 Forbidden")


def _client(**overrides: str) -> SharePointClient:
    params: dict[str, str] = {
        "remote": "sp:",
        "root": "Shared Documents",
        "config_path": "/enc/rclone.conf",
        "password_command": "pass rclone-config",
        "site_url": "https://tenant.sharepoint.com/sites/Team",
        "library_path": "",
    }
    params.update(overrides)
    return SharePointClient(SharePointRemoteSpec(**params))


class TestRemoteNormalisation:
    def test_remote_gains_trailing_colon(self) -> None:
        assert _client(remote="sp").remote == "sp:"

    def test_remote_keeps_existing_colon(self) -> None:
        assert _client(remote="sp:").remote == "sp:"

    def test_library_path_defaults_to_root(self) -> None:
        assert _client(root="Docs", library_path="").library_path == "Docs"

    def test_library_path_overrides_root(self) -> None:
        client = _client(root="Docs", library_path="sites/Team/Docs")
        assert client.library_path == "sites/Team/Docs"


class TestListFiles:
    def test_recursive_list_parses_lsjson(self) -> None:
        payload = '[{"Path": "a.txt", "Name": "a.txt", "IsDir": false}]'
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok(payload)) as run:
            records = _client().list_files("sub")

        assert records == [{"Path": "a.txt", "Name": "a.txt", "IsDir": False}]
        cmd = run.call_args.args[0]
        assert cmd == ["rclone", "--config", "/enc/rclone.conf", "lsjson", "sp:Shared Documents/sub", "--recursive"]

    def test_non_recursive_omits_flag(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("[]")) as run:
            _client().list_files(recursive=False)

        assert "--recursive" not in run.call_args.args[0]

    def test_empty_stdout_is_empty_list(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("")):
            assert _client().list_files() == []


class TestCatAndFetch:
    def test_cat_returns_stream(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("hello")) as run:
            assert _client().cat("dir/file.md") == "hello"

        assert run.call_args.args[0][-2:] == ["cat", "sp:Shared Documents/dir/file.md"]

    def test_fetch_copies_and_returns_dest(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok()) as run:
            dest = _client().fetch("dir/file.md", "/tmp/file.md")

        assert dest == "/tmp/file.md"
        assert run.call_args.args[0] == [
            "rclone",
            "--config",
            "/enc/rclone.conf",
            "copyto",
            "sp:Shared Documents/dir/file.md",
            "/tmp/file.md",
        ]


class TestShareLink:
    def test_derives_stable_id_deep_link(self) -> None:
        url = _client(root="Shared Documents", library_path="sites/Team/Shared Documents").share_link("Specs")
        assert url == (
            "https://tenant.sharepoint.com/sites/Team/_layouts/15/onedrive.aspx?id=/sites/Team/Shared%20Documents/Specs"
        )

    def test_link_at_library_root(self) -> None:
        url = _client(library_path="Docs").share_link()
        assert url.endswith("onedrive.aspx?id=/Docs")


class TestVerifyLink:
    def test_exists_true_when_path_resolves(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("[]")):
            result = _client(library_path="Docs").verify_link("Specs")

        assert result == {
            "path": "Specs",
            "url": "https://tenant.sharepoint.com/sites/Team/_layouts/15/onedrive.aspx?id=/Docs/Specs",
            "exists": True,
        }

    def test_exists_false_when_path_missing(self) -> None:
        with patch(
            "teatree.backends.sharepoint.run_checked",
            side_effect=_failed(["rclone", "lsjson"]),
        ):
            result = _client().verify_link("Ghost")

        assert result["exists"] is False


class TestVerifyReadOnly:
    def test_refused_write_confirms_read_only(self) -> None:
        with patch(
            "teatree.backends.sharepoint.run_checked",
            side_effect=_failed(["rclone", "mkdir"]),
        ):
            assert _client().verify_read_only() is True

    def test_accepted_write_raises(self) -> None:
        with (
            patch("teatree.backends.sharepoint.run_checked", return_value=_ok()),
            pytest.raises(RuntimeError, match="read-only contract violated"),
        ):
            _client().verify_read_only()


class TestEnvAndConfig:
    def test_password_command_rides_env_not_argv(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("[]")) as run:
            _client(password_command="pass swordfish-entry").list_files()

        env = run.call_args.kwargs["env"]
        assert env["RCLONE_PASSWORD_COMMAND"] == "pass swordfish-entry"
        assert env["PATH"]  # merged with the ambient environment, not a bare dict
        assert all("swordfish-entry" not in arg for arg in run.call_args.args[0])

    def test_no_password_command_passes_no_env(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("[]")) as run:
            _client(password_command="").list_files()

        assert run.call_args.kwargs["env"] is None

    def test_no_config_path_omits_config_flag(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("[]")) as run:
            _client(config_path="").list_files()

        assert "--config" not in run.call_args.args[0]

    def test_remote_root_only_when_no_subpath(self) -> None:
        with patch("teatree.backends.sharepoint.run_checked", return_value=_ok("[]")) as run:
            _client(root="").list_files(recursive=False)

        assert run.call_args.args[0][-2:] == ["lsjson", "sp:"]
