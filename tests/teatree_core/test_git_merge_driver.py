"""The ``generated`` merge driver merges like git, and says the doc needs regenerating.

A merge driver runs before git writes any of the merge result to the working tree, so
the tree it would regenerate from is still the LOCAL (ours) side. A generator run there
reproduces ours byte-for-byte and git records the generated doc as a CLEAN merge holding
ours — theirs discarded with no marker (souliane/teatree#4259). Four contracts:

-   the driver spawns no generator and resolves a driven path by git's own textual
    3-way merge;
-   a registered clone and an unregistered one produce the same merge result;
-   a driven path whose textual merge genuinely conflicts stays unmerged, with markers;
-   ``install_merge_driver`` writes the ``merge.generated.driver`` value into a
    checkout's ``.git/config``.
"""

import subprocess
import sys
from pathlib import Path

import git_merge_generated as driver

import teatree
from teatree.cli.setup.merge_driver_installer import GitMergeDriverInstaller
from teatree.core import git_merge_driver as merge_driver
from teatree.core.git_merge_driver import install_merge_driver
from tests._git_repo import make_git_repo, run_git, run_git_captured

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

_DRIVER_SCRIPT = driver.teatree_source_root() / "scripts" / "hooks" / "git_merge_generated.py"
_DRIVEN_DOC = "docs/generated/cli-reference.md"
_GENERATOR_RAN_MARKER = ".generator-ran"
_BASE_LINES = ["- t3 alpha", "- t3 bravo", "- t3 charlie", "- t3 delta", "- t3 echo"]

# A generator that rebuilds the doc from the checkout's own source, exactly as the real
# `generate_cli_reference.py` rebuilds it from the live command tree. Run inside a merge
# it reads the pre-merge (ours) tree, which is the whole defect.
_STUB_GENERATOR = """import pathlib, sys
pathlib.Path(".generator-ran").touch()
pathlib.Path(sys.argv[1]).write_text(pathlib.Path("src/commands.txt").read_text())
"""


def _doc_text(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


def _far_apart_lines(*, ours: bool) -> list[str]:
    """Ours edits the FIRST entry, theirs the LAST — far enough apart to merge cleanly."""
    lines = list(_BASE_LINES)
    lines[0 if ours else -1] = "- t3 alpha-OURS" if ours else "- t3 echo-THEIRS"
    return lines


def _same_line_lines(*, ours: bool) -> list[str]:
    lines = list(_BASE_LINES)
    lines[2] = "- t3 charlie-OURS" if ours else "- t3 charlie-THEIRS"
    return lines


def _write_side(repo: Path, *, side: str, doc_lines: list[str]) -> None:
    (repo / "src" / "commands.txt").write_text(_doc_text(doc_lines), encoding="utf-8")
    (repo / _DRIVEN_DOC).write_text(_doc_text(doc_lines), encoding="utf-8")
    (repo / "src" / "thing.py").write_text(f"VALUE = {side!r}\n", encoding="utf-8")


def _merge_repo(root: Path, *, register: bool, doc_conflicts: bool = False) -> Path:
    """A repo whose merge conflicts in ``src/`` while the driven doc merges cleanly.

    ``doc_conflicts`` makes both sides edit the SAME doc line instead, so the textual
    3-way merge of the doc itself conflicts.
    """
    sides = _same_line_lines if doc_conflicts else _far_apart_lines
    repo = make_git_repo(root)
    (repo / "src").mkdir()
    (repo / "docs" / "generated").mkdir(parents=True)
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "hooks" / "generate_cli_reference.py").write_text(_STUB_GENERATOR, encoding="utf-8")
    (repo / ".gitattributes").write_text(f"{_DRIVEN_DOC} merge=generated\n", encoding="utf-8")

    _write_side(repo, side="BASE", doc_lines=_BASE_LINES)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "base")

    run_git(repo, "checkout", "-q", "-b", "theirs")
    _write_side(repo, side="THEIRS", doc_lines=sides(ours=False))
    run_git(repo, "commit", "-q", "-am", "theirs")

    run_git(repo, "checkout", "-q", "main")
    run_git(repo, "checkout", "-q", "-b", "ours")
    _write_side(repo, side="OURS", doc_lines=sides(ours=True))
    run_git(repo, "commit", "-q", "-am", "ours")

    if register:
        run_git(
            repo,
            "config",
            "merge.generated.driver",
            f"{sys.executable} {_DRIVER_SCRIPT} %O %A %B %P",
        )
    return repo


def _merge_theirs(repo: Path) -> subprocess.CompletedProcess[str]:
    return run_git_captured(repo, "merge", "theirs")


class TestRegistrationIsNotAMergeVariable:
    """The same merge must land the same bytes whether or not the driver is registered.

    A merge driver whose behaviour depends on whether provisioning happened to run is
    not a reproducible build step: two contributors get two different merge results
    from one merge (souliane/teatree#4259).
    """

    def test_registered_and_unregistered_arms_agree(self, tmp_path):
        registered = _merge_repo(tmp_path / "registered", register=True)
        control = _merge_repo(tmp_path / "control", register=False)

        _merge_theirs(registered)
        _merge_theirs(control)

        assert (registered / _DRIVEN_DOC).read_text(encoding="utf-8") == (
            (control / _DRIVEN_DOC).read_text(encoding="utf-8")
        )
        assert run_git(registered, "hash-object", _DRIVEN_DOC) == run_git(control, "hash-object", _DRIVEN_DOC)

    def test_the_other_side_survives_the_merge(self, tmp_path):
        registered = _merge_repo(tmp_path / "registered", register=True)

        _merge_theirs(registered)

        merged = (registered / _DRIVEN_DOC).read_text(encoding="utf-8")
        assert "- t3 echo-THEIRS" in merged, (
            "the merge silently resolved the generated doc to ours — the other side's entry is gone "
            "with no conflict, no marker and no warning."
        )
        assert "- t3 alpha-OURS" in merged

    def test_the_src_conflict_the_defect_needs_is_really_there(self, tmp_path):
        """The control that keeps the two assertions above from passing vacuously."""
        registered = _merge_repo(tmp_path / "registered", register=True)

        _merge_theirs(registered)

        assert "src/thing.py" in run_git(registered, "diff", "--name-only", "--diff-filter=U")


class TestTheDriverRunsNoGenerator:
    """Regeneration inside a driver reads the PRE-merge tree, so the driver must not try.

    Git decides every content merge before it writes any of the result to the working
    tree — measured on a merge whose source files did not even conflict.
    """

    def test_no_generator_is_spawned_during_a_merge(self, tmp_path):
        registered = _merge_repo(tmp_path / "registered", register=True)

        _merge_theirs(registered)

        assert not (registered / _GENERATOR_RAN_MARKER).exists(), (
            "the driver spawned a generator mid-merge; it can only see the ours-side tree there, "
            "so its output is ours dressed up as a clean merge."
        )

    def test_the_merge_says_the_doc_needs_regenerating(self, tmp_path):
        registered = _merge_repo(tmp_path / "registered", register=True)

        merge = _merge_theirs(registered)

        assert "generate_cli_reference.py" in merge.stderr, (
            f"the merge said nothing about the generated doc being stale: {merge.stderr!r}"
        )


class TestAGenuineDocConflictStaysConflicted:
    def test_both_arms_leave_the_doc_unmerged_with_markers(self, tmp_path):
        registered = _merge_repo(tmp_path / "registered", register=True, doc_conflicts=True)
        control = _merge_repo(tmp_path / "control", register=False, doc_conflicts=True)

        _merge_theirs(registered)
        _merge_theirs(control)

        for repo in (registered, control):
            assert _DRIVEN_DOC in run_git(repo, "diff", "--name-only", "--diff-filter=U")
            assert "<<<<<<<" in (repo / _DRIVEN_DOC).read_text(encoding="utf-8")


class TestDriverMain:
    def _slots(self, tmp_path: Path, *, base: str, ours: str, theirs: str) -> tuple[str, str, str]:
        base_slot = tmp_path / "base"
        ours_slot = tmp_path / "ours"
        theirs_slot = tmp_path / "theirs"
        base_slot.write_text(base, encoding="utf-8")
        ours_slot.write_text(ours, encoding="utf-8")
        theirs_slot.write_text(theirs, encoding="utf-8")
        return str(base_slot), str(ours_slot), str(theirs_slot)

    def _far_apart(self, tmp_path: Path) -> tuple[str, str, str]:
        return self._slots(
            tmp_path,
            base="one\ntwo\nthree\n",
            ours="one-OURS\ntwo\nthree\n",
            theirs="one\ntwo\nthree-THEIRS\n",
        )

    def test_a_registered_path_merges_both_sides(self, tmp_path):
        base, ours_slot, theirs = self._far_apart(tmp_path)

        rc = driver.main([base, ours_slot, theirs, _DRIVEN_DOC])

        assert rc == 0
        assert Path(ours_slot).read_text(encoding="utf-8") == "one-OURS\ntwo\nthree-THEIRS\n"

    def test_a_hand_maintained_path_merges_both_sides(self, tmp_path):
        base, ours_slot, theirs = self._far_apart(tmp_path)

        rc = driver.main([base, ours_slot, theirs, "evals/README.md"])

        assert rc == 0
        assert "three-THEIRS" in Path(ours_slot).read_text(encoding="utf-8"), (
            "keeping ours for a driven path with no generator silently discards the other side's edits."
        )

    def test_an_unknown_path_merges_both_sides(self, tmp_path):
        base, ours_slot, theirs = self._far_apart(tmp_path)

        rc = driver.main([base, ours_slot, theirs, "some/other/file.md"])

        assert rc == 0
        assert "three-THEIRS" in Path(ours_slot).read_text(encoding="utf-8")

    def test_an_overlapping_edit_reports_a_conflict(self, tmp_path):
        base, ours_slot, theirs = self._slots(tmp_path, base="mid\n", ours="mid-OURS\n", theirs="mid-THEIRS\n")

        rc = driver.main([base, ours_slot, theirs, _DRIVEN_DOC])

        assert rc == 1
        assert "<<<<<<<" in Path(ours_slot).read_text(encoding="utf-8")

    def test_an_unreadable_slot_reports_a_conflict(self, tmp_path):
        base, ours_slot, _theirs = self._far_apart(tmp_path)

        rc = driver.main([base, ours_slot, str(tmp_path / "absent"), _DRIVEN_DOC])

        assert rc == 1

    def test_too_few_arguments_is_an_error(self):
        assert driver.main(["only", "three", "args"]) == 2

    def test_registered_paths_cover_the_gitattributes_entries(self):
        assert _DRIVEN_DOC in driver.registered_paths()
        assert "evals/README.md" in driver.registered_paths()

    def test_registered_paths_cover_the_management_commands_doc(self):
        assert "docs/generated/management-commands.md" in driver.registered_paths()


class TestRegenerationAdvisory:
    def test_a_generator_backed_path_names_its_generator(self):
        assert "scripts/hooks/generate_cli_reference.py" in driver.regeneration_advisory(_DRIVEN_DOC)

    def test_a_hand_maintained_path_names_itself_but_no_generator(self):
        advisory = driver.regeneration_advisory("evals/README.md")

        assert "evals/README.md" in advisory
        assert "generate_" not in advisory

    def test_an_unknown_path_has_no_advisory(self):
        assert driver.regeneration_advisory("some/other/file.md") == ""


class TestVendoredLayout:
    """``%P`` arrives OUTER-repo-relative when core is vendored (souliane/teatree#3582).

    git passes ``%P`` relative to the top of the working tree, and the advisory keys are
    relative to teatree's own root. In a fork that vendors core those differ by the
    vendoring prefix, so a generated doc merged with no advisory at all.
    """

    def _outer_layout(self) -> tuple[Path, str]:
        """An outer repo root one level above teatree's root, plus that prefix."""
        root = driver.teatree_source_root().resolve()
        return root.parent, root.name

    def test_outer_relative_path_maps_onto_a_generator_key(self):
        outer, prefix = self._outer_layout()
        mapped = driver.teatree_relative_path(f"{prefix}/{_DRIVEN_DOC}", repo_root=outer)

        assert mapped == _DRIVEN_DOC
        assert mapped in driver.registered_paths()

    def test_path_outside_the_vendored_tree_is_untouched(self):
        outer, _prefix = self._outer_layout()

        assert driver.teatree_relative_path("overlay/docs/notes.md", repo_root=outer) == "overlay/docs/notes.md"

    def test_plain_clone_path_is_untouched(self):
        root = driver.teatree_source_root().resolve()

        assert driver.teatree_relative_path(_DRIVEN_DOC, repo_root=root) == _DRIVEN_DOC

    def test_main_advises_for_an_outer_relative_path(self, tmp_path, monkeypatch, capsys):
        outer, prefix = self._outer_layout()
        slots = {"base": "one\n", "ours": "one-OURS\n", "theirs": "one\n"}
        for name, text in slots.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        monkeypatch.chdir(outer)

        rc = driver.main(
            [str(tmp_path / "base"), str(tmp_path / "ours"), str(tmp_path / "theirs"), f"{prefix}/{_DRIVEN_DOC}"]
        )

        assert rc == 0
        assert "generate_cli_reference.py" in capsys.readouterr().err


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
    that does not exist there, and a driver git cannot run emits no regeneration
    advisory at all.
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


class TestRepoGitattributes:
    def test_repo_gitattributes_marks_the_generated_docs(self):
        repo_root = Path(__file__).resolve().parents[2]
        for path in (_DRIVEN_DOC, "evals/README.md"):
            attr = run_git(repo_root, "check-attr", "merge", "--", path)
            assert attr.endswith("merge: generated"), f"{path} not marked merge=generated: {attr!r}"
