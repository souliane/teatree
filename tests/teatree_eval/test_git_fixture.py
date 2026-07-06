"""The ``git_repo`` clean-room fixture provisions the state its prompts presuppose.

A scenario tagged ``fixture: git_repo`` runs against a real throwaway repo. The
fixture must provision exactly the working-tree state the tagged prompts assert:
an ``origin`` remote (so ``origin/main`` / ``git merge-base`` resolve), a
``feat/example`` branch two commits ahead, one staged uncommitted change, and the
seeded helper files. ``test_new_code_ships_with_tests`` presupposes a helper at
``src/teatree/util/money.py`` the agent "just wrote" — without that file the agent
finds an empty cwd and investigates the mismatch instead of writing its test, a
false negative. This pins that money.py is present and un-tested so the gap the
scenario asks the agent to close is real.
"""

import pytest

from teatree.eval.git_fixture import KNOWN_FIXTURES, provision_git_fixture
from teatree.utils.git_run import run_strict as git


class TestProvisionGitFixture:
    def test_seeds_the_money_helper_the_new_code_scenario_presupposes(self) -> None:
        with provision_git_fixture("git_repo") as repo:
            money = repo / "src" / "teatree" / "util" / "money.py"
            assert money.is_file(), "git_repo fixture must seed src/teatree/util/money.py"
            body = money.read_text(encoding="utf-8")
            assert "def to_cents(" in body
            assert "def format_money(" in body

    def test_money_helper_ships_without_a_test_so_the_gap_is_real(self) -> None:
        # The scenario asks the agent to ADD the missing test; if the fixture
        # already shipped one, the scenario would be vacuous.
        with provision_git_fixture("git_repo") as repo:
            assert not (repo / "tests").exists()
            assert not list(repo.rglob("test_money.py"))

    def test_money_helper_is_committed_so_it_is_in_the_working_tree(self) -> None:
        with provision_git_fixture("git_repo") as repo:
            tracked = git(repo=str(repo), args=["ls-files", "src/teatree/util/money.py"]).strip()
            assert tracked == "src/teatree/util/money.py"

    def test_still_provisions_the_origin_remote_and_squash_branch(self) -> None:
        # The money.py addition must not disturb the state the ship scenarios rely on.
        with provision_git_fixture("git_repo") as repo:
            remotes = git(repo=str(repo), args=["remote"]).split()
            assert "origin" in remotes
            branch = git(repo=str(repo), args=["rev-parse", "--abbrev-ref", "HEAD"]).strip()
            assert branch == "feat/example"
            staged = git(repo=str(repo), args=["diff", "--cached", "--name-only"]).split()
            assert "feature_c.py" in staged

    def test_unknown_fixture_kind_is_rejected(self) -> None:
        assert "git_repo" in KNOWN_FIXTURES
        with pytest.raises(ValueError, match="nope"), provision_git_fixture("nope"):
            pass
