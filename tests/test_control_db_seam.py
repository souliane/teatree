"""ONE control-DB resolution seam every subcommand shares (#3514).

From a non-provisioned worktree, subcommands disagreed about which database they
were talking to: the Django/ORM path auto-isolates a worktree onto a per-worktree
DB, while the pre-Django cold path always resolves the PRIMARY one. Two answers,
no seam, no signal — so a ticket written by one subcommand was invisible to the
next and got stranded between the two.

:class:`teatree.paths.ControlDb` is that seam. Both paths derive from it,
so the env precedence (``T3_CONFIG_DB`` → ``XDG_DATA_HOME`` → ``~/.local/share``)
has exactly one implementation, and the divergence is a REPORTED fact rather than
an invisible one.
"""

from pathlib import Path

import pytest

from teatree.config.cold_db import canonical_config_db
from teatree.paths import ControlDb, ControlDbResolution


def _worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "wt"
    repo.mkdir(exist_ok=True)
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    return repo


def _clone(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    return repo


class TestOneSeam:
    def test_primary_resolution_is_not_isolated(self, tmp_path: Path) -> None:
        resolved = ControlDb({}, tmp_path).for_repo(_clone(tmp_path))
        assert isinstance(resolved, ControlDbResolution)
        assert resolved.isolated is False
        assert resolved.path == tmp_path / ".local" / "share" / "teatree" / "db.sqlite3"

    def test_explicit_config_db_wins_everywhere(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit.sqlite3"
        env = {"T3_CONFIG_DB": str(explicit)}
        assert ControlDb(env, tmp_path).for_repo(_clone(tmp_path)).path == explicit
        assert ControlDb(env, tmp_path).for_repo(_worktree(tmp_path)).path == explicit

    def test_xdg_data_home_is_honoured(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        resolved = ControlDb({"XDG_DATA_HOME": str(sandbox)}, tmp_path).for_repo(_clone(tmp_path))
        assert resolved.path == sandbox / "teatree" / "db.sqlite3"

    def test_a_worktree_without_explicit_xdg_is_isolated_and_says_so(self, tmp_path: Path) -> None:
        resolved = ControlDb({}, tmp_path).for_repo(_worktree(tmp_path))
        assert resolved.isolated is True
        assert resolved.reason


class TestColdPathSharesTheSeam:
    """``canonical_config_db`` derives from the seam rather than re-implementing it."""

    def test_agrees_with_the_seam_on_a_primary_clone(self, tmp_path: Path) -> None:
        seam = ControlDb({}, tmp_path).for_repo(_clone(tmp_path))
        assert canonical_config_db(env={}, home=tmp_path) == seam.path

    def test_agrees_with_the_seam_on_an_explicit_override(self, tmp_path: Path) -> None:
        env = {"T3_CONFIG_DB": str(tmp_path / "explicit.sqlite3")}
        seam = ControlDb(env, tmp_path).for_repo(_clone(tmp_path))
        assert canonical_config_db(env=env, home=tmp_path) == seam.path


class TestDivergenceIsReportedNotSilent:
    def test_no_divergence_on_a_primary_clone(self, tmp_path: Path) -> None:
        assert ControlDb({}, tmp_path).divergence_message(_clone(tmp_path)) is None

    def test_an_isolated_worktree_reports_both_paths_and_the_remedy(self, tmp_path: Path) -> None:
        message = ControlDb({}, tmp_path).divergence_message(_worktree(tmp_path))
        assert message is not None
        assert str(ControlDb({}, tmp_path).for_repo(_worktree(tmp_path)).path) in message
        assert str(canonical_config_db(env={}, home=tmp_path)) in message
        assert "worktree provision" in message

    def test_an_explicit_override_collapses_the_two_and_reports_nothing(self, tmp_path: Path) -> None:
        env = {"T3_CONFIG_DB": str(tmp_path / "explicit.sqlite3")}
        assert ControlDb(env, tmp_path).divergence_message(_worktree(tmp_path)) is None


@pytest.mark.parametrize("repo_kind", ["clone", "worktree"])
def test_the_seam_is_pure_and_repeatable(tmp_path: Path, repo_kind: str) -> None:
    repo = _clone(tmp_path) if repo_kind == "clone" else _worktree(tmp_path)
    first = ControlDb({}, tmp_path).for_repo(repo)
    assert ControlDb({}, tmp_path).for_repo(repo) == first
