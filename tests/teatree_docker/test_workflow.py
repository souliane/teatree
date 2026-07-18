"""Tests for the containerized-``t3`` workflow pure logic (#3232).

The alias line, marker-delimited managed block, idempotent rc install, and the
wired-state health probe are shared by ``t3 setup`` (the installer) and
``t3 doctor`` (the verifier), so they are unit-tested here in one place.
"""

from pathlib import Path

from teatree.docker.workflow import (
    ALIAS_MARKER_BEGIN,
    ALIAS_MARKER_END,
    AliasInstall,
    alias_line,
    compose_path,
    install_alias_block,
    installed_alias_block,
    is_running_in_container,
    render_alias_block,
    workflow_problems,
    wrapper_path,
)


class TestAliasRendering:
    def test_alias_line_points_at_repo_wrapper(self, tmp_path: Path) -> None:
        line = alias_line(tmp_path)
        assert line == f'alias t3="{(tmp_path / "deploy" / "t3").resolve()}"'

    def test_block_is_marker_delimited(self, tmp_path: Path) -> None:
        block = render_alias_block(tmp_path)
        assert block.startswith(ALIAS_MARKER_BEGIN)
        assert ALIAS_MARKER_END in block
        assert alias_line(tmp_path) in block


class TestIsRunningInContainer:
    def test_true_when_teatree_role_set(self) -> None:
        assert is_running_in_container({"TEATREE_ROLE": "worker"}, dockerenv=Path("/nope")) is True

    def test_true_when_dockerenv_marker_present(self, tmp_path: Path) -> None:
        marker = tmp_path / ".dockerenv"
        marker.write_text("", encoding="utf-8")
        assert is_running_in_container({}, dockerenv=marker) is True

    def test_false_on_a_plain_host(self, tmp_path: Path) -> None:
        assert is_running_in_container({}, dockerenv=tmp_path / "absent") is False


class TestInstallAliasBlock:
    def test_creates_missing_rc_with_just_the_block(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        assert install_alias_block(rc, tmp_path) is AliasInstall.INSTALLED
        assert rc.read_text(encoding="utf-8") == render_alias_block(tmp_path)

    def test_appends_to_existing_rc_preserving_content(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        rc.write_text("export FOO=1\n", encoding="utf-8")
        assert install_alias_block(rc, tmp_path) is AliasInstall.INSTALLED
        text = rc.read_text(encoding="utf-8")
        assert text.startswith("export FOO=1\n")
        assert ALIAS_MARKER_BEGIN in text

    def test_appends_newline_when_rc_lacks_trailing_newline(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        rc.write_text("export FOO=1", encoding="utf-8")  # no trailing newline
        install_alias_block(rc, tmp_path)
        assert "export FOO=1\n" + ALIAS_MARKER_BEGIN in rc.read_text(encoding="utf-8")

    def test_rerun_is_idempotent_already_present(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        install_alias_block(rc, tmp_path)
        assert install_alias_block(rc, tmp_path) is AliasInstall.ALREADY_PRESENT

    def test_refreshes_a_stale_path_in_place(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        old_repo = tmp_path / "old-clone"
        rc.write_text("export FOO=1\n" + render_alias_block(old_repo) + "export BAR=2\n", encoding="utf-8")
        new_repo = tmp_path / "new-clone"
        assert install_alias_block(rc, new_repo) is AliasInstall.UPDATED
        text = rc.read_text(encoding="utf-8")
        assert alias_line(new_repo) in text
        assert alias_line(old_repo) not in text
        # Surrounding lines survive, and the block does not accumulate blanks.
        assert "export FOO=1\n" in text
        assert "export BAR=2\n" in text
        assert text.count(ALIAS_MARKER_BEGIN) == 1

    def test_unwritable_when_rc_parent_is_a_file(self, tmp_path: Path) -> None:
        wall = tmp_path / "wall"
        wall.write_text("", encoding="utf-8")
        assert install_alias_block(wall / ".bashrc", tmp_path) is AliasInstall.UNWRITABLE


class TestInstalledAliasBlock:
    def test_returns_block_from_first_rc_that_has_it(self, tmp_path: Path) -> None:
        bashrc = tmp_path / ".bashrc"
        zshrc = tmp_path / ".zshrc"
        install_alias_block(zshrc, tmp_path)
        found = installed_alias_block([bashrc, zshrc])
        assert found is not None
        assert alias_line(tmp_path) in found

    def test_returns_none_when_no_rc_carries_the_block(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        rc.write_text("export FOO=1\n", encoding="utf-8")
        assert installed_alias_block([rc, tmp_path / "absent"]) is None


class TestWorkflowProblems:
    def _wire_repo(self, root: Path) -> Path:
        repo = root / "clone"
        (repo / "deploy").mkdir(parents=True)
        compose_path(repo).write_text("name: teatree\n", encoding="utf-8")
        wrapper = wrapper_path(repo)
        wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        wrapper.chmod(0o755)
        return repo

    def test_healthy_workflow_has_no_problems(self, tmp_path: Path) -> None:
        repo = self._wire_repo(tmp_path)
        block = render_alias_block(repo)
        assert workflow_problems(repo, block, lambda _tool: "/usr/bin/docker") == []

    def test_flags_missing_docker_cli(self, tmp_path: Path) -> None:
        repo = self._wire_repo(tmp_path)
        block = render_alias_block(repo)
        problems = workflow_problems(repo, block, lambda _tool: None)
        assert any("docker" in p for p in problems)

    def test_flags_missing_compose_and_wrapper(self, tmp_path: Path) -> None:
        repo = tmp_path / "bare"
        (repo / "deploy").mkdir(parents=True)
        block = render_alias_block(repo)
        problems = workflow_problems(repo, block, lambda _tool: "/usr/bin/docker")
        assert any("compose" in p for p in problems)
        assert any("wrapper missing" in p for p in problems)

    def test_flags_non_executable_wrapper(self, tmp_path: Path) -> None:
        repo = self._wire_repo(tmp_path)
        wrapper_path(repo).chmod(0o644)
        problems = workflow_problems(repo, render_alias_block(repo), lambda _tool: "/usr/bin/docker")
        assert any("not executable" in p for p in problems)

    def test_flags_stale_alias_path(self, tmp_path: Path) -> None:
        repo = self._wire_repo(tmp_path)
        stale_block = render_alias_block(tmp_path / "somewhere-else")
        problems = workflow_problems(repo, stale_block, lambda _tool: "/usr/bin/docker")
        assert any("stale" in p for p in problems)
