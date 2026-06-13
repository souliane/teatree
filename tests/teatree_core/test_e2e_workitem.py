"""Tests for the durable e2e work-item recipe, ladder, reconcile, provenance.

Issue #794 MVP: ``t3 <overlay> e2e run <work-item>`` — one command that runs
the e2e for a work item with auto-provisioning from a DB-durable recipe keyed
by ``issue_url``.

These run on the production SQLite backend (django.test.TestCase) per the
#804 lesson, with real ``git`` under ``tmp_path``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.e2e_workitem import E2ERecipe, RepoEntry, load_recipe, record_run, resolve_environment, save_recipe
from teatree.core.models import Ticket, Worktree

_GIT = shutil.which("git") or "git"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        [_GIT, "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo_with_commits(path: Path, n: int = 2) -> list[str]:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t.test")
    _git(path, "config", "user.name", "T")
    shas: list[str] = []
    for i in range(n):
        (path / f"f{i}").write_text(str(i))
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(path, "rev-parse", "HEAD"))
    return shas


class RecipeRoundTripTests(TestCase):
    def test_recipe_persists_to_ticket_extra_keyed_by_issue_url(self) -> None:
        ticket = Ticket.objects.create(
            overlay="demo",
            issue_url="https://github.com/o/r/issues/794",
        )
        recipe = E2ERecipe(
            repos=[RepoEntry(repo="o/r", branch="feat", last_green_sha="deadbeef")],
            last_run=None,
        )

        save_recipe(ticket, recipe)
        reloaded = load_recipe(Ticket.objects.get(pk=ticket.pk))

        assert reloaded.repos == [RepoEntry(repo="o/r", branch="feat", last_green_sha="deadbeef")]
        # Durable: it survived a DB round-trip keyed only by the work item.
        assert Ticket.objects.resolve("794").extra["e2e_recipe"]["repos"][0]["repo"] == "o/r"

    def test_load_recipe_is_empty_for_a_fresh_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="demo", issue_url="https://x/issues/1")

        assert load_recipe(ticket) == E2ERecipe(repos=[], last_run=None)


class LadderResolutionTests(TestCase):
    """The default environment ladder.

    1. existing workspace fully present on disk → use as-is
    2. else last-green SHA-set → provision at those SHAs
    3. else origin/main
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="demo", issue_url="https://x/issues/42")

    def test_rung1_existing_workspace_used_as_is(self) -> None:
        wt_dir = Path(self._tmp()) / "r"
        _init_repo_with_commits(wt_dir, 1)
        Worktree.objects.create(
            ticket=self.ticket,
            overlay="demo",
            repo_path="r",
            branch="feat",
            extra={"worktree_path": str(wt_dir)},
        )

        res = resolve_environment(self.ticket)

        assert res.rung == "existing"
        assert res.repo_dirs == {"r": str(wt_dir)}

    def test_rung2_last_green_when_workspace_missing(self) -> None:
        # Recipe knows a green SHA but no worktree exists on disk.
        save_recipe(
            self.ticket,
            E2ERecipe(
                repos=[RepoEntry(repo="r", branch="feat", last_green_sha="abc123")],
                last_run=None,
            ),
        )

        res = resolve_environment(self.ticket)

        assert res.rung == "last_green"
        assert res.provision_at == {"r": "abc123"}

    def test_explicit_at_last_green_forces_recorded_green_set(self) -> None:
        # Workspace IS present, but --at last-green must skip the existing
        # rung and provision at the recorded green SHA-set instead.
        wt_dir = Path(self._tmp()) / "r"
        _init_repo_with_commits(wt_dir, 1)
        Worktree.objects.create(
            ticket=self.ticket,
            overlay="demo",
            repo_path="r",
            branch="feat",
            extra={"worktree_path": str(wt_dir)},
        )
        save_recipe(
            self.ticket,
            E2ERecipe(
                repos=[RepoEntry(repo="r", branch="feat", last_green_sha="green-sha")],
                last_run=None,
            ),
        )

        res = resolve_environment(self.ticket, at="last-green")

        assert res.rung == "last_green"
        assert res.provision_at == {"r": "green-sha"}

    def test_rung3_origin_main_when_no_recipe_and_no_workspace(self) -> None:
        self.ticket.repos = ["r"]
        self.ticket.save(update_fields=["repos"])

        res = resolve_environment(self.ticket)

        assert res.rung == "main"
        assert res.provision_at == {"r": "origin/main"}

    def test_explicit_at_main_overrides_existing_workspace(self) -> None:
        wt_dir = Path(self._tmp()) / "r"
        _init_repo_with_commits(wt_dir, 1)
        Worktree.objects.create(
            ticket=self.ticket,
            overlay="demo",
            repo_path="r",
            branch="feat",
            extra={"worktree_path": str(wt_dir)},
        )

        res = resolve_environment(self.ticket, at="main")

        assert res.rung == "main"
        assert res.provision_at == {"r": "origin/main"}

    def test_reconcile_on_read_db_path_missing_falls_through(self) -> None:
        # DB row claims a path that does not exist on disk → must NOT be
        # treated as "existing"; fall through the ladder (never run anyway).
        Worktree.objects.create(
            ticket=self.ticket,
            overlay="demo",
            repo_path="r",
            branch="feat",
            extra={"worktree_path": str(Path(self._tmp()) / "gone")},
        )
        self.ticket.repos = ["r"]
        self.ticket.save(update_fields=["repos"])

        res = resolve_environment(self.ticket)

        assert res.rung != "existing"

    def _tmp(self) -> str:
        import tempfile  # noqa: PLC0415

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class ProvenanceTests(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="demo", issue_url="https://x/issues/7")

    def test_green_run_promotes_sha_set_to_last_green(self) -> None:
        record_run(self.ticket, result="green", per_repo_shas={"r": "sha-green"})

        recipe = load_recipe(Ticket.objects.get(pk=self.ticket.pk))
        assert recipe.repos == [RepoEntry(repo="r", branch="", last_green_sha="sha-green")]
        assert recipe.last_run is not None
        assert recipe.last_run["result"] == "green"
        assert recipe.last_run["per_repo_shas"] == {"r": "sha-green"}

    def test_failed_run_records_provenance_but_never_becomes_baseline(self) -> None:
        save_recipe(
            self.ticket,
            E2ERecipe(
                repos=[RepoEntry(repo="r", branch="feat", last_green_sha="old-green")],
                last_run=None,
            ),
        )

        record_run(self.ticket, result="red", per_repo_shas={"r": "sha-bad"})

        recipe = load_recipe(Ticket.objects.get(pk=self.ticket.pk))
        # last_green is untouched by a failed run …
        assert recipe.repos == [RepoEntry(repo="r", branch="feat", last_green_sha="old-green")]
        # … but the failure is still auditable.
        assert recipe.last_run is not None
        assert recipe.last_run["result"] == "red"
        assert recipe.last_run["per_repo_shas"] == {"r": "sha-bad"}

    def test_provenance_has_a_timestamp(self) -> None:
        record_run(self.ticket, result="green", per_repo_shas={"r": "s"})

        recipe = load_recipe(self.ticket)
        assert recipe.last_run is not None
        assert "timestamp" in recipe.last_run
        assert recipe.last_run["timestamp"]

    def test_env_defaults_to_local(self) -> None:
        record_run(self.ticket, result="green", per_repo_shas={"r": "s"})

        recipe = load_recipe(self.ticket)
        assert recipe.last_run is not None
        assert recipe.last_run["env"] == "local"

    def test_dev_env_is_recorded(self) -> None:
        record_run(self.ticket, result="green", per_repo_shas={"r": "s"}, env="dev")

        recipe = load_recipe(self.ticket)
        assert recipe.last_run is not None
        assert recipe.last_run["env"] == "dev"


class GitHelpersTests(TestCase):
    def test_head_sha_returns_full_sha(self, tmp_path: Path | None = None) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.utils import git  # noqa: PLC0415

        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(d), ignore_errors=True))
        shas = _init_repo_with_commits(d, 1)

        assert git.head_sha(repo=str(d)) == shas[0]

    def test_worktree_add_at_ref_checks_out_the_exact_sha(self) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.utils import git  # noqa: PLC0415

        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(base), ignore_errors=True))
        repo = base / "repo"
        shas = _init_repo_with_commits(repo, 3)
        target = base / "wt"

        ok = git.worktree_add_at_ref(repo=str(repo), path=str(target), ref=shas[1])

        assert ok is True
        assert _git(target, "rev-parse", "HEAD") == shas[1]

    def test_repo_head_sha_wrapper_returns_full_sha(self) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.utils import git  # noqa: PLC0415

        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(d), ignore_errors=True))
        shas = _init_repo_with_commits(d, 1)

        assert git.GitRepo(str(d)).head_sha() == shas[0]

    def test_repo_worktree_add_at_ref_wrapper_checks_out_the_exact_sha(self) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.utils import git  # noqa: PLC0415

        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(base), ignore_errors=True))
        repo = base / "repo"
        shas = _init_repo_with_commits(repo, 3)
        target = base / "wt"

        ok = git.GitRepo(str(repo)).worktree_add_at_ref(str(target), shas[1])

        assert ok is True
        assert _git(target, "rev-parse", "HEAD") == shas[1]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
