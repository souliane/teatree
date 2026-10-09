"""The invocation cwd crosses the container boundary as data, or degrades to ``Path.cwd``."""

from pathlib import Path

import pytest

from teatree.core.invocation_cwd import INVOCATION_CWD_ENV, invocation_cwd, operator_cwd
from tests._git_repo import make_git_repo


class TestInvocationCwd:
    def test_falls_back_to_the_process_cwd_when_nothing_is_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(INVOCATION_CWD_ENV, raising=False)
        monkeypatch.chdir(tmp_path)

        assert invocation_cwd() == Path.cwd()

    def test_declared_directory_wins_over_the_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        declared = tmp_path / "where-the-operator-stood"
        declared.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(declared))

        assert invocation_cwd() == declared

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_declaration_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(INVOCATION_CWD_ENV, value)

        assert invocation_cwd() == Path.cwd()

    def test_a_path_that_is_not_a_directory_here_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stale or untranslated value names something meaningless on this side;
        # trusting it would silently redirect the command.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(tmp_path / "no-such-dir"))

        assert invocation_cwd() == Path.cwd()

    def test_a_file_is_not_accepted_as_a_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        a_file = tmp_path / "not-a-dir"
        a_file.touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(a_file))

        assert invocation_cwd() == Path.cwd()

    def test_discarding_an_unusable_declaration_names_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Degrading in silence lands back on the process cwd — the original bug.

        The ship refusals tell operators to export this variable, so a value that
        is discarded without a word makes that instruction unfollowable.
        """
        unusable = tmp_path / "host-side-path"
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(unusable))

        with caplog.at_level("WARNING", logger="teatree.core.invocation_cwd"):
            assert invocation_cwd() == Path.cwd()

        assert str(unusable) in caplog.text
        assert INVOCATION_CWD_ENV in caplog.text


class TestOperatorCwd:
    """An agent inherits the declaration of the session that spawned it, so a checkout it stands in wins."""

    def test_a_process_cwd_inside_a_checkout_wins_over_an_inherited_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = make_git_repo(tmp_path / "checkout")
        (checkout / "sub").mkdir()
        declared = tmp_path / "where-the-session-started"
        declared.mkdir()
        monkeypatch.chdir(checkout / "sub")
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(declared))

        assert operator_cwd() == checkout / "sub"

    def test_a_process_cwd_outside_any_checkout_defers_to_the_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workdir = tmp_path / "container-workdir"
        workdir.mkdir()
        declared = make_git_repo(tmp_path / "checkout")
        monkeypatch.chdir(workdir)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(declared))

        assert operator_cwd() == declared

    def test_nothing_declared_is_the_process_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv(INVOCATION_CWD_ENV, raising=False)

        assert operator_cwd() == tmp_path

    def test_an_unusable_declaration_is_the_process_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(INVOCATION_CWD_ENV, str(tmp_path / "no-such-dir"))

        assert operator_cwd() == tmp_path
