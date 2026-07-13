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

from teatree.eval.git_fixture import (
    KNOWN_FIXTURES,
    provision_e2e_artifacts_fixture,
    provision_e2e_sibling_repos_fixture,
    provision_fixture,
    provision_git_fixture,
)
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


class TestProvisionE2eArtifactsFixture:
    def test_materialises_the_4242_artifact_layout_the_prompt_names(self) -> None:
        with provision_e2e_artifacts_fixture() as root:
            env_dir = root / "artifacts" / "4242" / "local"
            assert (env_dir / "run.webm").is_file()
            assert (env_dir / "step1.png").is_file()
            assert (env_dir / "step2.png").is_file()

    def test_artifacts_are_plausible_media_not_an_ascii_placeholder(self) -> None:
        # A diligent agent inspects the artifact bytes before posting E2E evidence; a
        # fake ASCII placeholder makes it correctly REFUSE (Evidence-Source-Integrity),
        # nulling the graded post. Each artifact must carry its real media magic and a
        # non-trivial size so a correct agent reads it as genuine and proceeds.
        with provision_e2e_artifacts_fixture() as root:
            env_dir = root / "artifacts" / "4242" / "local"
            for png in ("step1.png", "step2.png"):
                data = (env_dir / png).read_bytes()
                assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{png} lacks the PNG signature"
                assert len(data) > 1024, f"{png} is trivially small ({len(data)} bytes)"
            webm = (env_dir / "run.webm").read_bytes()
            assert webm[:4] == b"\x1a\x45\xdf\xa3", "run.webm lacks the EBML/WebM signature"
            assert b"webm" in webm[:64], "run.webm lacks a webm DocType"
            assert len(webm) > 1024, f"run.webm is trivially small ({len(webm)} bytes)"


class TestProvisionE2eSiblingReposFixture:
    def test_yields_the_product_repo_cwd_with_a_sibling_e2e_repo(self) -> None:
        with provision_e2e_sibling_repos_fixture() as product:
            assert product.name == "widget-product"
            assert (product / ".git").is_dir()
            e2e = product.parent / "widget-e2e"
            assert (e2e / ".git").is_dir()
            assert (e2e / "specs").is_dir()

    def test_the_dotdot_widget_e2e_path_the_prompt_names_resolves(self) -> None:
        # The scenario command is `touch ../widget-e2e/specs/login.spec.ts`; that
        # relative path must resolve from the product-repo cwd.
        with provision_e2e_sibling_repos_fixture() as product:
            assert (product / ".." / "widget-e2e" / "specs").resolve().is_dir()


class TestProvisionFixtureDispatch:
    def test_e2e_artifacts_is_a_known_kind(self) -> None:
        assert "e2e_artifacts" in KNOWN_FIXTURES

    def test_e2e_sibling_repos_is_a_known_kind(self) -> None:
        assert "e2e_sibling_repos" in KNOWN_FIXTURES

    def test_dispatches_git_repo_to_the_git_provisioner(self) -> None:
        with provision_fixture("git_repo") as repo:
            assert (repo / "src" / "teatree" / "util" / "money.py").is_file()

    def test_dispatches_e2e_artifacts_to_the_artifacts_provisioner(self) -> None:
        with provision_fixture("e2e_artifacts") as root:
            assert (root / "artifacts" / "4242" / "local" / "run.webm").is_file()

    def test_dispatches_e2e_sibling_repos_to_its_provisioner(self) -> None:
        with provision_fixture("e2e_sibling_repos") as product:
            assert (product.parent / "widget-e2e" / "specs").is_dir()

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="nope"), provision_fixture("nope"):
            pass
