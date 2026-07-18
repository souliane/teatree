"""Tests for ``DockerAliasInstaller`` — the ``t3 setup`` alias-wiring unit (#3232).

``t3 setup`` wires the containerized ``t3`` alias into the operator's shell rc
files. It manages ``~/.bashrc`` always and ``~/.zshrc`` only when it exists, and
no-ops inside a container (there the container IS the CLI). Best-effort — an
unwritable rc WARNs and never aborts setup.
"""

from pathlib import Path
from unittest.mock import patch

from teatree.cli.setup.docker_alias import DockerAliasInstaller
from teatree.docker.workflow import ALIAS_MARKER_BEGIN, alias_line


class TestTargetRcFiles:
    def test_bashrc_always_zshrc_only_when_present(self, tmp_path: Path) -> None:
        installer = DockerAliasInstaller(tmp_path, home=tmp_path)
        assert installer.target_rc_files() == [tmp_path / ".bashrc"]

        (tmp_path / ".zshrc").write_text("", encoding="utf-8")
        assert installer.target_rc_files() == [tmp_path / ".bashrc", tmp_path / ".zshrc"]


class TestInstall:
    def test_writes_alias_into_bashrc_on_a_host(self, tmp_path: Path) -> None:
        repo = tmp_path / "clone"
        home = tmp_path / "op_home"
        home.mkdir()
        messages: list[str] = []
        with patch("teatree.cli.setup.docker_alias.is_running_in_container", return_value=False):
            DockerAliasInstaller(repo, home=home).install(echo=messages.append)

        bashrc = home / ".bashrc"
        assert ALIAS_MARKER_BEGIN in bashrc.read_text(encoding="utf-8")
        assert alias_line(repo) in bashrc.read_text(encoding="utf-8")
        assert any("Installed containerized t3 alias" in m for m in messages)

    def test_noop_inside_container(self, tmp_path: Path) -> None:
        home = tmp_path / "op_home"
        home.mkdir()
        messages: list[str] = []
        with patch("teatree.cli.setup.docker_alias.is_running_in_container", return_value=True):
            DockerAliasInstaller(tmp_path, home=home).install(echo=messages.append)

        assert not (home / ".bashrc").exists()
        assert any("Containerized runtime" in m for m in messages)

    def test_warns_but_does_not_raise_when_rc_unwritable(self, tmp_path: Path) -> None:
        repo = tmp_path / "clone"
        home = tmp_path / "op_home"
        # A .bashrc that is a directory makes the write fail -> UNWRITABLE, not a raise.
        home.mkdir()
        (home / ".bashrc").mkdir()
        messages: list[str] = []
        with patch("teatree.cli.setup.docker_alias.is_running_in_container", return_value=False):
            DockerAliasInstaller(repo, home=home).install(echo=messages.append)
        assert any("WARN" in m and "not writable" in m for m in messages)
