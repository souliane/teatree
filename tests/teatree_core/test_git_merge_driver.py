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

import teatree
from teatree.cli.setup.merge_driver_installer import GitMergeDriverInstaller
from teatree.core import git_merge_driver as merge_driver
from teatree.core.git_merge_driver import install_merge_driver
from tests._git_repo import make_git_repo, run_git

# The literal git config value the driver contract is pinned to. Asserting the
# installed value against the production constant would be tautological — both
# sides would read the same string, so a broken command could never fail here.
# The script path is ABSOLUTE here because the checkouts these tests install into are
# scratch repos that do not contain the driver script — the fallback arm. The relative
# arm, which is what a real clone or fork takes, is pinned by
# `TestDriverCommandIsCheckoutRelative`. The root is re-derived here from the installed
# package rather than through `teatree_source_root`, so the shape stays independently
# pinned.
_TEATREE_ROOT = Path(teatree.__file__).resolve().parents[2]
_EXPECTED_DRIVER_COMMAND = f"uv run python {_TEATREE_ROOT}/scripts/hooks/git_merge_generated.py %O %A %B %P"


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


class TestVendoredLayout:
    """``%P`` arrives OUTER-repo-relative when core is vendored (souliane/teatree#3582).

    git passes ``%P`` relative to the top of the working tree, and the generator keys
    are relative to teatree's own root. In a fork that vendors core those differ by the
    vendoring prefix, so every generated doc missed the lookup and resolved by silently
    keeping ours — a worse outcome than the textual conflict it replaced.
    """

    def _outer_layout(self) -> tuple[Path, str]:
        """An outer repo root one level above teatree's root, plus that prefix."""
        root = driver.teatree_source_root().resolve()
        return root.parent, root.name

    def test_outer_relative_path_maps_onto_a_generator_key(self):
        outer, prefix = self._outer_layout()
        mapped = driver.teatree_relative_path(f"{prefix}/docs/generated/cli-reference.md", repo_root=outer)

        assert mapped == "docs/generated/cli-reference.md"
        assert mapped in driver.registered_paths()

    def test_path_outside_the_vendored_tree_is_untouched(self):
        outer, _prefix = self._outer_layout()

        assert driver.teatree_relative_path("overlay/docs/notes.md", repo_root=outer) == "overlay/docs/notes.md"

    def test_plain_clone_path_is_untouched(self):
        root = driver.teatree_source_root().resolve()

        assert (
            driver.teatree_relative_path("docs/generated/cli-reference.md", repo_root=root)
            == "docs/generated/cli-reference.md"
        )

    def test_main_regenerates_for_an_outer_relative_path(self, tmp_path, monkeypatch):
        outer, prefix = self._outer_layout()
        ours_slot = tmp_path / "ours"
        ours_slot.write_text("OURS\n", encoding="utf-8")

        def fake_regenerate(generator_argv: list[str], output_path: str) -> bool:
            assert generator_argv == ["scripts/hooks/generate_cli_reference.py"]
            Path(output_path).write_text("REGENERATED\n", encoding="utf-8")
            return True

        monkeypatch.setattr(driver, "_regenerate", fake_regenerate)
        monkeypatch.chdir(outer)
        rc = driver.main(["base", str(ours_slot), "theirs", f"{prefix}/docs/generated/cli-reference.md"])

        assert rc == 0
        assert ours_slot.read_text(encoding="utf-8") == "REGENERATED\n"


class TestDriverCommandNamesARealScript:
    def test_the_registered_command_points_at_an_existing_script(self, tmp_path):
        repo = make_git_repo(tmp_path / "repo")
        install_merge_driver(repo)
        configured = run_git(repo, "config", "--get", "merge.generated.driver")
        script = Path(configured.split()[3])

        assert script.is_absolute(), f"driver script outside the checkout must be absolute, got {script}"
        assert script.is_file(), f"driver script does not exist: {script}"


class TestDriverCommandIsCheckoutRelative:
    """A driver script INSIDE the checkout is named relative to it.

    ``.git/config`` is per-clone, and a clone bind-mounted into a container is one
    config file reachable at two different absolute paths. An absolute command let
    whichever side registered the driver last point the other side's merges at a path
    that does not exist there — and git answers a missing driver by falling back to a
    textual 3-way merge, hand-resolving a generated doc, which is precisely what
    marking these paths ``merge=generated`` exists to prevent.
    """

    def _fake_source_root(self, monkeypatch, root: Path) -> Path:
        (root / "scripts" / "hooks").mkdir(parents=True)
        (root / "scripts" / "hooks" / "git_merge_generated.py").touch()
        monkeypatch.setattr(merge_driver, "teatree_source_root", lambda: root)
        return root

    def test_a_vendored_script_is_named_relative_to_the_fork_root(self, tmp_path, monkeypatch):
        checkout = tmp_path / "fork"
        self._fake_source_root(monkeypatch, checkout / "vendor" / "teatree")

        assert merge_driver.driver_command(checkout) == (
            "uv run python vendor/teatree/scripts/hooks/git_merge_generated.py %O %A %B %P"
        )

    def test_a_plain_clone_names_the_script_at_its_own_root(self, tmp_path, monkeypatch):
        checkout = tmp_path / "clone"
        self._fake_source_root(monkeypatch, checkout)

        assert merge_driver.driver_command(checkout) == (
            "uv run python scripts/hooks/git_merge_generated.py %O %A %B %P"
        )

    def test_a_script_outside_the_checkout_stays_absolute(self, tmp_path, monkeypatch):
        self._fake_source_root(monkeypatch, tmp_path / "elsewhere")

        script = Path(merge_driver.driver_command(tmp_path / "repo").split()[3])

        assert script.is_absolute(), f"a script outside the checkout has no relative form, got {script}"

    def test_the_two_checkouts_of_one_bind_mount_agree(self, tmp_path, monkeypatch):
        """The host path and the container path of the same tree yield ONE command."""
        host = tmp_path / "host" / "downstream-fork"
        self._fake_source_root(monkeypatch, host / "vendor" / "teatree")
        host_command = merge_driver.driver_command(host)

        container = tmp_path / "home" / "teatree" / "teatree"
        self._fake_source_root(monkeypatch, container / "vendor" / "teatree")

        assert merge_driver.driver_command(container) == host_command


class TestInstallMergeDriver:
    def test_writes_driver_config_into_checkout(self, tmp_path):
        repo = make_git_repo(tmp_path / "repo")
        line = install_merge_driver(repo)

        assert line.startswith("OK")
        configured = run_git(repo, "config", "--get", "merge.generated.driver")
        assert configured == _EXPECTED_DRIVER_COMMAND
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
            assert run_git(repo, "config", "--get", "merge.generated.driver") == _EXPECTED_DRIVER_COMMAND


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


class TestManagementCommandsRegeneratesThroughTheRealGenerator:
    """A genuine 3-way merge of ``management-commands.md`` regenerates, not "ours".

    ``TestEndToEndConflictResolution`` above drives the merge through a STUB
    generator, so it proves the driver's plumbing but says nothing about whether a
    registered generator actually fills the ``%A`` slot. That gap is what let a
    generator which derived its destination from ``argv[0].parent`` pass every
    existing test while silently resolving each merge to "ours". This drives the
    real ``generate_management_commands_doc.py`` end to end.
    """

    def test_real_generator_fills_the_output_slot_on_a_real_merge(self, tmp_path):
        repo = make_git_repo(tmp_path / "repo")
        real_root = Path(__file__).resolve().parents[2]
        # The driver spawns its generator by REPO-RELATIVE path, resolved against the
        # merge's cwd, so the temp repo needs the same scripts/hooks layout.
        (repo / "scripts").symlink_to(real_root / "scripts")
        (repo / ".git" / "info" / "exclude").write_text("/scripts\n", encoding="utf-8")

        gen = repo / "docs" / "generated" / "management-commands.md"
        gen.parent.mkdir(parents=True)
        (repo / ".gitattributes").write_text("docs/generated/management-commands.md merge=generated\n")
        run_git(
            repo,
            "config",
            "merge.generated.driver",
            f"{sys.executable} {real_root / 'scripts' / 'hooks' / 'git_merge_generated.py'} %O %A %B %P",
        )

        gen.write_text("# Management commands\n\n- base\n", encoding="utf-8")
        run_git(repo, "add", ".gitattributes", "docs")
        run_git(repo, "commit", "-q", "-m", "base")

        run_git(repo, "checkout", "-q", "-b", "feat-a")
        gen.write_text("# Management commands\n\n- base\n- OURS-ONLY\n", encoding="utf-8")
        run_git(repo, "commit", "-q", "-am", "ours")

        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "checkout", "-q", "-b", "feat-b")
        gen.write_text("# Management commands\n\n- base\n- THEIRS-ONLY\n", encoding="utf-8")
        run_git(repo, "commit", "-q", "-am", "theirs")

        run_git(repo, "merge", "feat-a", check=False)

        merged = gen.read_text(encoding="utf-8")
        assert "<<<<<<<" not in merged, f"merge left conflict markers: {merged!r}"
        assert ">>>>>>>" not in merged, f"merge left conflict markers: {merged!r}"
        assert "OURS-ONLY" not in merged, (
            "the merge resolved to the un-regenerated 'ours' side — the driver reported success "
            "without the generator ever writing its output slot."
        )
        assert "THEIRS-ONLY" not in merged
        assert "lifecycle" in merged, (
            "the merged file must hold the REGENERATED reference built from the live command tree."
        )

    def test_a_real_merge_leaves_no_generated_litter_in_the_repo_root(self, tmp_path):
        repo = make_git_repo(tmp_path / "repo")
        real_root = Path(__file__).resolve().parents[2]
        (repo / "scripts").symlink_to(real_root / "scripts")
        (repo / ".git" / "info" / "exclude").write_text("/scripts\n", encoding="utf-8")

        gen = repo / "docs" / "generated" / "management-commands.md"
        gen.parent.mkdir(parents=True)
        (repo / ".gitattributes").write_text("docs/generated/management-commands.md merge=generated\n")
        run_git(
            repo,
            "config",
            "merge.generated.driver",
            f"{sys.executable} {real_root / 'scripts' / 'hooks' / 'git_merge_generated.py'} %O %A %B %P",
        )

        gen.write_text("# Management commands\n\n- base\n", encoding="utf-8")
        run_git(repo, "add", ".gitattributes", "docs")
        run_git(repo, "commit", "-q", "-m", "base")

        run_git(repo, "checkout", "-q", "-b", "feat-a")
        gen.write_text("# Management commands\n\n- base\n- a\n", encoding="utf-8")
        run_git(repo, "commit", "-q", "-am", "ours")

        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "checkout", "-q", "-b", "feat-b")
        gen.write_text("# Management commands\n\n- base\n- b\n", encoding="utf-8")
        run_git(repo, "commit", "-q", "-am", "theirs")

        run_git(repo, "merge", "feat-a", check=False)

        # git's %A slot lives in the repo ROOT; a generator that writes beside it
        # rather than into it drops these there, where nothing reads them.
        for stray in ("management-commands.md", "management-commands.json"):
            assert not (repo / stray).exists(), (
                f"the merge dropped {stray} in the repo root — that is the un-tracked writer that "
                "re-dirties a working tree mid-rebase."
            )

    def test_registered_paths_cover_the_management_commands_doc(self):
        assert "docs/generated/management-commands.md" in driver.registered_paths()
