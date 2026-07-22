"""The ``generated`` git merge driver resolves generated-doc conflicts by regeneration.

Never by textual 3-way merge (souliane/teatree#3582). Three contracts:

-   the driver script (``scripts/hooks/git_merge_generated.py``) regenerates a
    registered path into the ``%A`` output slot and keeps ours for a
    no-generator or unknown path;
-   ``install_merge_driver`` writes the ``merge.generated.driver`` value into a
    checkout's ``.git/config``;
-   end-to-end, a simulated ``cli-reference.md``-shaped conflict on a
    ``merge=generated`` path resolves via the driver with no conflict markers.
"""

import sys
from pathlib import Path

import git_merge_generated as driver
import pytest

from teatree.cli.setup.merge_driver_installer import GitMergeDriverInstaller
from teatree.core.git_merge_driver import install_merge_driver, merge_driver_command
from tests._git_repo import make_git_repo, run_git


def _forbidden_regenerate(generator_argv: list[str], output_path: str) -> bool:
    return pytest.fail("_regenerate must not run for a keep-ours path")


class TestDriverMain:
    def _slots(self, tmp_path: Path, *, ours: str = "OURS\n") -> tuple[str, str, str]:
        base = tmp_path / "base"
        ours_slot = tmp_path / "ours"
        theirs = tmp_path / "theirs"
        base.write_text("BASE\n", encoding="utf-8")
        ours_slot.write_text(ours, encoding="utf-8")
        theirs.write_text("THEIRS\n", encoding="utf-8")
        return str(base), str(ours_slot), str(theirs)

    def test_regenerates_registered_path_into_ours_slot(self, tmp_path, monkeypatch):
        base, ours_slot, theirs = self._slots(tmp_path)

        def fake_regenerate(generator_argv: list[str], output_path: str) -> bool:
            assert generator_argv == ["scripts/hooks/generate_cli_reference.py"]
            Path(output_path).write_text("REGENERATED\n", encoding="utf-8")
            return True

        monkeypatch.setattr(driver, "_regenerate", fake_regenerate)
        rc = driver.main([base, ours_slot, theirs, "docs/generated/cli-reference.md"])

        assert rc == 0
        assert Path(ours_slot).read_text(encoding="utf-8") == "REGENERATED\n"

    def test_no_generator_path_keeps_ours(self, tmp_path, monkeypatch):
        base, ours_slot, theirs = self._slots(tmp_path)
        monkeypatch.setattr(driver, "_regenerate", _forbidden_regenerate)
        rc = driver.main([base, ours_slot, theirs, "evals/README.md"])

        assert rc == 0
        assert Path(ours_slot).read_text(encoding="utf-8") == "OURS\n"

    def test_unknown_path_keeps_ours(self, tmp_path, monkeypatch):
        base, ours_slot, theirs = self._slots(tmp_path)
        monkeypatch.setattr(driver, "_regenerate", _forbidden_regenerate)
        rc = driver.main([base, ours_slot, theirs, "some/other/file.md"])

        assert rc == 0
        assert Path(ours_slot).read_text(encoding="utf-8") == "OURS\n"

    def test_regenerate_failure_returns_conflict(self, tmp_path, monkeypatch):
        base, ours_slot, theirs = self._slots(tmp_path)
        monkeypatch.setattr(driver, "_regenerate", lambda _argv, _out: False)
        rc = driver.main([base, ours_slot, theirs, "docs/generated/cli-reference.md"])

        assert rc == 1

    def test_too_few_arguments_is_an_error(self):
        assert driver.main(["only", "three", "args"]) == 2

    def test_registered_paths_cover_the_gitattributes_entries(self):
        assert "docs/generated/cli-reference.md" in driver.registered_paths()
        assert "evals/README.md" in driver.registered_paths()


class TestInstallMergeDriver:
    def test_writes_driver_config_into_checkout(self, tmp_path):
        repo = make_git_repo(tmp_path / "repo")
        line = install_merge_driver(repo)

        assert line.startswith("OK")
        configured = run_git(repo, "config", "--get", "merge.generated.driver")
        assert configured == merge_driver_command()
        assert run_git(repo, "config", "--get", "merge.generated.name")

    def test_missing_git_dir_degrades_to_warn(self, tmp_path):
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        line = install_merge_driver(not_a_repo)

        assert line.startswith("WARN")


class TestGitMergeDriverInstaller:
    def test_installs_into_every_given_checkout(self, tmp_path):
        repos = [make_git_repo(tmp_path / "a"), make_git_repo(tmp_path / "b")]
        echoed: list[str] = []

        GitMergeDriverInstaller(repos[0], checkouts=repos).install(echo=echoed.append)

        assert len(echoed) == len(repos)
        assert all(line.startswith("OK") for line in echoed)
        for repo in repos:
            assert run_git(repo, "config", "--get", "merge.generated.driver") == merge_driver_command()


class TestEndToEndConflictResolution:
    """A real git merge on a ``merge=generated`` path resolves via the driver."""

    def test_simulated_cli_reference_conflict_regenerates(self, tmp_path):
        repo = make_git_repo(tmp_path / "repo")
        gen = repo / "docs" / "generated" / "cli-reference.md"
        gen.parent.mkdir(parents=True)

        (repo / ".gitattributes").write_text("docs/generated/cli-reference.md merge=generated\n")
        # A stub driver stands in for the real regenerator: it discards both
        # sides and writes deterministic content to the %A output slot, exactly
        # as the real driver does after running the CLI-reference generator.
        stub = tmp_path / "stub_driver.py"
        stub.write_text("import sys, pathlib\npathlib.Path(sys.argv[2]).write_text('REGENERATED\\n')\n")
        run_git(repo, "config", "merge.generated.driver", f"{sys.executable} {stub} %O %A %B %P")

        gen.write_text("# CLI reference\n\n- t3 base-cmd\n")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "base")

        run_git(repo, "checkout", "-q", "-b", "feat-a")
        gen.write_text("# CLI reference\n\n- t3 base-cmd\n- t3 cmd-a\n")
        run_git(repo, "commit", "-q", "-am", "add cmd-a")

        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "checkout", "-q", "-b", "feat-b")
        gen.write_text("# CLI reference\n\n- t3 base-cmd\n- t3 cmd-b\n")
        run_git(repo, "commit", "-q", "-am", "add cmd-b")

        # Both branches touched the same adjacent line — a textual merge would
        # conflict. The driver must resolve it instead.
        run_git(repo, "merge", "feat-a", check=False)

        merged = gen.read_text()
        assert "<<<<<<<" not in merged
        assert "=======" not in merged
        assert ">>>>>>>" not in merged
        assert merged == "REGENERATED\n"

    def test_repo_gitattributes_marks_the_generated_docs(self):
        repo_root = Path(__file__).resolve().parents[2]
        for path in ("docs/generated/cli-reference.md", "evals/README.md"):
            attr = run_git(repo_root, "check-attr", "merge", "--", path)
            assert attr.endswith("merge: generated"), f"{path} not marked merge=generated: {attr!r}"
