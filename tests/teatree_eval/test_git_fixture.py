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

import os
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.core.evidence.test_plan_validation import has_red_highlight_box, validate_test_plan_images
from teatree.core.evidence.video_evidence import check_video_evidence
from teatree.eval.git_fixture import (
    KNOWN_FIXTURES,
    provision_e2e_artifacts_fixture,
    provision_e2e_sibling_repos_fixture,
    provision_fixture,
    provision_git_fixture,
    provision_uv_project_fixture,
)
from teatree.utils.git_run import run_strict as git

#: This dev repo's OWN ``teatree`` is editable-installed via a meta-path finder
#: that wins "import teatree" regardless of sys.path/``PYTHONPATH`` order
#: (verified empirically — prepending a throwaway ``teatree`` dir via
#: ``PYTHONPATH`` still resolves to this repo's real package). Running the
#: fixture's own pytest with ``-S`` (skip ``site.py``, so that finder never
#: registers) + ``PYTHONPATH`` pointed at pytest's site-packages dir isolates
#: the check to the mechanism the fixture actually relies on — its own
#: ``pyproject.toml`` ``pythonpath = ["src"]`` — with no interference from
#: this repo's self-referential install.
_PYTEST_SITE_DIR = str(Path(pytest.__file__).resolve().parent.parent)


def _run_pytest_isolated(repo: Path, node: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-S", "-m", "pytest", node, "-q"],
        cwd=repo,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": _PYTEST_SITE_DIR},
        capture_output=True,
        text=True,
        check=False,
    )


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

    def test_artifacts_carry_real_media_magic(self) -> None:
        # A diligent agent inspects the artifact bytes before posting E2E evidence; a
        # fake ASCII placeholder makes it correctly REFUSE (Evidence-Source-Integrity).
        # Each artifact carries its real media magic and a non-trivial size.
        with provision_e2e_artifacts_fixture() as root:
            env_dir = root / "artifacts" / "4242" / "local"
            for png in ("step1.png", "step2.png"):
                data = (env_dir / png).read_bytes()
                assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{png} lacks the PNG signature"
                assert len(data) > 1024, f"{png} is trivially small ({len(data)} bytes)"
            webm = (env_dir / "run.webm").read_bytes()
            assert webm[:4] == b"\x1a\x45\xdf\xa3", "run.webm lacks the EBML/WebM signature"
            assert len(webm) > 1024, f"run.webm is trivially small ({len(webm)} bytes)"

    def test_screenshots_are_byte_distinct(self) -> None:
        # post-test-plan's md5 dedup gate refuses two byte-identical images, and a
        # diligent agent runs that check before posting — identical captures make it
        # correctly refuse and never issue the canonical command (the #3190 regression).
        with provision_e2e_artifacts_fixture() as root:
            env_dir = root / "artifacts" / "4242" / "local"
            assert (env_dir / "step1.png").read_bytes() != (env_dir / "step2.png").read_bytes()

    def test_screenshots_clear_the_real_image_gates(self) -> None:
        # The bytes must pass the SAME gates post-test-plan enforces (red-box pixel
        # count + byte-identical dedup), not merely look like a PNG to `file`. A
        # capture short of the red-box floor reads as box-less evidence a correct
        # agent refuses to post.
        with provision_e2e_artifacts_fixture() as root:
            env_dir = root / "artifacts" / "4242" / "local"
            images = [env_dir / "step1.png", env_dir / "step2.png"]
            for image in images:
                assert has_red_highlight_box(image), f"{image.name} lacks the red highlight box"
            assert validate_test_plan_images(images) == []

    def test_recording_is_a_parseable_recording_where_ffprobe_is_present(self) -> None:
        # On a host WITH ffmpeg/ffprobe (a `--local` metered run) the agent probes the
        # recording; an unparsable file (ffprobe returns no duration) reads as corrupt
        # and it refuses. A real clip probes to a positive duration with no dead lead.
        # Where the tool is absent (the CI image installs none) the check skips cleanly,
        # so realness is only assertable when the tool is present.
        with provision_e2e_artifacts_fixture() as root:
            report = check_video_evidence(root / "artifacts" / "4242" / "local" / "run.webm")
            if not report.skipped:
                assert report.duration > 0, "run.webm is not a parseable recording"
                assert report.ok, report.detail


class TestProvisionUvProjectFixture:
    def test_provisions_a_real_pyproject_toml_with_src_on_the_pythonpath(self) -> None:
        with provision_uv_project_fixture() as repo:
            pyproject = repo / "pyproject.toml"
            assert pyproject.is_file()
            body = pyproject.read_text(encoding="utf-8")
            assert 'pythonpath = ["src"]' in body

    def test_seeds_the_money_helper_at_the_path_the_prompt_names(self) -> None:
        with provision_uv_project_fixture() as repo:
            money = repo / "src" / "teatree" / "util" / "money.py"
            assert money.is_file()
            assert "def add(" in money.read_text(encoding="utf-8")

    def test_the_tests_mirror_dir_exists_so_the_agents_write_lands_cleanly(self) -> None:
        with provision_uv_project_fixture() as repo:
            assert (repo / "tests" / "teatree" / "util").is_dir()
            assert not list(repo.rglob("test_money.py")), "no test must pre-exist — the gap must be real"

    def test_a_mirror_test_against_the_seeded_helper_actually_passes(self) -> None:
        # The behaviour this fixture exists for: the agent writes a mirror test
        # importing the seeded helper, runs it, and it genuinely goes green —
        # no PYTHONPATH/import-path confusion, no install step required.
        with provision_uv_project_fixture() as repo:
            test_file = repo / "tests" / "teatree" / "util" / "test_money.py"
            test_file.write_text(
                "from teatree.util.money import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            result = _run_pytest_isolated(repo, "tests/teatree/util/test_money.py")
            assert result.returncode == 0, result.stdout + result.stderr
            assert "1 passed" in result.stdout

    def test_uv_project_is_a_known_kind(self) -> None:
        assert "uv_project" in KNOWN_FIXTURES


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

    def test_dispatches_uv_project_to_its_provisioner(self) -> None:
        with provision_fixture("uv_project") as repo:
            assert (repo / "pyproject.toml").is_file()
            assert (repo / "src" / "teatree" / "util" / "money.py").is_file()

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="nope"), provision_fixture("nope"):
            pass
