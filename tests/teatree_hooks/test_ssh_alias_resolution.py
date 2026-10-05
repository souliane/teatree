"""OpenSSH computes aliases across config syntax and included files."""

from pathlib import Path

import pytest

from teatree.hooks import _ssh_alias
from teatree.hooks._repo_visibility import slug_for_remote_url


@pytest.mark.parametrize(
    ("config_text", "include_text"),
    [
        ("Include {include}\n", "Host bank-alias\n  HostName gitlab.com\n"),
        ("Host bank-*\n  HostName gitlab.com\n", ""),
        ("Host bank-alias\n  HostName=gitlab.com\n", ""),
        ("Match host bank-alias\n  HostName gitlab.com\n", ""),
    ],
)
def test_ssh_config_forms_resolve_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_text: str, include_text: str
) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    include = ssh_dir / "included.conf"
    include.write_text(include_text, encoding="utf-8")
    (ssh_dir / "config").write_text(config_text.format(include=include), encoding="utf-8")
    monkeypatch.setattr("teatree.hooks._repo_visibility.Path.home", lambda: tmp_path)
    assert slug_for_remote_url("git@bank-alias:acme/widget.git") == "gitlab.com/acme/widget"


class TestTheSystemConfigIsReadToo:
    """``ssh -F`` replaces BOTH default configs, so the temporary one has to include the system file itself."""

    @pytest.fixture
    def system_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "etc-ssh" / "ssh_config"
        path.parent.mkdir()
        monkeypatch.setattr(_ssh_alias, "SYSTEM_SSH_CONFIG", path)
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        monkeypatch.setattr("teatree.hooks._repo_visibility.Path.home", lambda: home)
        return path

    def test_an_alias_only_the_system_config_defines_resolves(self, system_config: Path) -> None:
        system_config.write_text("Host sys-alias\n  HostName gitlab.example.test\n", encoding="utf-8")

        assert slug_for_remote_url("git@sys-alias:acme/widget.git") == "gitlab.example.test/acme/widget"

    def test_the_home_config_outranks_the_system_one(self, system_config: Path) -> None:
        system_config.write_text("Host both-alias\n  HostName system.example.test\n", encoding="utf-8")
        home_config = Path(_ssh_alias.Path.home()) / ".ssh" / "config"
        home_config.write_text("Host both-alias\n  HostName home.example.test\n", encoding="utf-8")

        assert slug_for_remote_url("git@both-alias:acme/widget.git") == "home.example.test/acme/widget"
