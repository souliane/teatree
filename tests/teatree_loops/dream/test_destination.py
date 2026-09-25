"""Grounding a dream cluster's ``durable_destination`` against the core checkout (#2663).

Pass 1 already grounds a cluster's CITATIONS against the extract's snippets. The
destination — the field that decides whether a row is promoted to a scheduled fix —
had no grounding at all, so an invented path scheduled a coding task for a file that
does not exist, and a real core path outside the prefix list was silently kept as
memory.
"""

from pathlib import Path

import pytest

from teatree.loops.dream import destination
from teatree.loops.dream.destination import classify_destination, points_at_core_fix


@pytest.fixture
def core_tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "teatree" / "loops" / "dream").mkdir(parents=True)
    (tmp_path / "src" / "teatree" / "loops" / "dream" / "promote_memory.py").touch()
    (tmp_path / "evals" / "scenarios").mkdir(parents=True)
    (tmp_path / "evals" / "scenarios" / "rules.yaml").touch()
    (tmp_path / "scripts" / "hooks").mkdir(parents=True)
    (tmp_path / "BLUEPRINT.md").touch()
    return tmp_path


class TestGhostDestinationsAreNotCoreFixes:
    def test_an_invented_package_is_not_a_core_fix(self, core_tree: Path) -> None:
        assert points_at_core_fix("src/teatree/ghost_pkg/ghost.py", root=core_tree) is False

    def test_the_verdict_names_why_it_is_ungrounded(self, core_tree: Path) -> None:
        verdict = classify_destination("src/teatree/ghost_pkg/ghost.py", root=core_tree)

        assert verdict.in_core_tree is False
        assert "src/teatree/ghost_pkg" in verdict.reason


class TestRealCoreTreePathsAreCoreFixes:
    def test_an_existing_file_is_a_core_fix(self, core_tree: Path) -> None:
        assert points_at_core_fix("src/teatree/loops/dream/promote_memory.py", root=core_tree) is True

    def test_an_existing_directory_is_a_core_fix(self, core_tree: Path) -> None:
        assert points_at_core_fix("src/teatree", root=core_tree) is True

    def test_a_new_file_in_an_existing_package_is_a_core_fix(self, core_tree: Path) -> None:
        assert points_at_core_fix("src/teatree/loops/dream/new_validator.py", root=core_tree) is True

    def test_a_core_path_outside_the_legacy_prefix_list_is_a_core_fix(self, core_tree: Path) -> None:
        assert points_at_core_fix("evals/scenarios/rules.yaml", root=core_tree) is True

    def test_an_absent_leaf_in_a_real_package_is_deliberately_still_a_core_fix(self, core_tree: Path) -> None:
        # No filesystem signal separates "the model invented this module" from "the fix
        # creates this module", and rejecting both loses every new-file gap — so the
        # grounding line is the PACKAGE, not the leaf.
        assert points_at_core_fix("src/teatree/loops/dream/not_written_yet.py", root=core_tree) is True

    def test_the_verdict_carries_the_repo_relative_form(self, core_tree: Path) -> None:
        verdict = classify_destination("  SRC/teatree/loops/dream/promote_memory.py  ", root=core_tree)

        assert verdict.rel_path == "src/teatree/loops/dream/promote_memory.py"


class TestMemoryDestinationsStayMemory:
    @pytest.mark.parametrize("destination", ["feedback_tone.md", "memory/topic.md", "", "   "])
    def test_a_memory_home_is_not_a_core_fix(self, destination: str, core_tree: Path) -> None:
        assert points_at_core_fix(destination, root=core_tree) is False

    def test_a_bare_root_filename_needs_the_file_itself_to_exist(self, core_tree: Path) -> None:
        assert points_at_core_fix("BLUEPRINT.md", root=core_tree) is True
        assert points_at_core_fix("feedback_tone.md", root=core_tree) is False


class TestCaseAndWhitespaceTolerance:
    """The distiller emits sloppy destinations; the pre-#2663 predicate lowercased them."""

    def test_a_mis_cased_directory_component_still_resolves(self, core_tree: Path) -> None:
        assert points_at_core_fix("  SCRIPTS/hooks/x.py  ", root=core_tree) is True

    def test_a_mis_cased_root_document_still_resolves(self, core_tree: Path) -> None:
        assert points_at_core_fix("blueprint.md", root=core_tree) is True

    def test_a_case_only_ambiguity_is_refused_rather_than_guessed(self, core_tree: Path) -> None:
        (core_tree / "Docs").mkdir()
        (core_tree / "docs").mkdir()

        verdict = classify_destination("DOCS/guide.md", root=core_tree)

        assert verdict.in_core_tree is False
        assert "ambiguous" in verdict.reason


class TestContainment:
    def test_an_absolute_path_inside_the_tree_is_rewritten_relative(self, core_tree: Path) -> None:
        absolute = str(core_tree / "src" / "teatree" / "loops" / "dream" / "promote_memory.py")

        verdict = classify_destination(absolute, root=core_tree)

        assert verdict.in_core_tree is True
        assert verdict.rel_path == "src/teatree/loops/dream/promote_memory.py"

    def test_an_absolute_path_outside_the_tree_is_not_a_core_fix(self, core_tree: Path, tmp_path: Path) -> None:
        outside = tmp_path.parent / "memory" / "feedback_tone.md"

        assert points_at_core_fix(str(outside), root=core_tree) is False

    def test_a_parent_traversal_escape_is_not_a_core_fix(self, core_tree: Path) -> None:
        assert points_at_core_fix("src/../../etc/passwd", root=core_tree) is False

    def test_a_symlink_escaping_the_tree_is_not_a_core_fix(self, core_tree: Path, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-core-tree"
        outside.mkdir(exist_ok=True)
        (core_tree / "escape").symlink_to(outside, target_is_directory=True)

        assert points_at_core_fix("escape/leak.py", root=core_tree) is False


class TestGroundableIncludesMemoryReferences:
    """A memory-shaped destination promotes rather than being withheld as ungrounded (#4776).

    Before #4776 every ``memory/<slug>.md`` destination was treated as ungrounded and
    kept as memory forever — 49 of 49 gaps measured on one pass were withheld this
    exact way. ``groundable`` (``in_core_tree`` OR ``is_memory_reference``) is the
    promotable test; ``in_core_tree`` itself is UNCHANGED (still False for a memory
    path — the compliance recurrence-redirect needs that narrower question).
    """

    def test_a_memory_reference_is_groundable(self, core_tree: Path) -> None:
        verdict = classify_destination("memory/topic.md", root=core_tree)
        assert verdict.is_memory_reference is True
        assert verdict.groundable is True

    def test_a_memory_reference_is_still_not_in_core_tree(self, core_tree: Path) -> None:
        # Unchanged behavior preservation: compliance's recurrence-redirect asks
        # in_core_tree alone and must keep treating a memory destination as
        # MEMORY_ONLY, or a recurring rule stops escalating to a structural fix.
        verdict = classify_destination("memory/topic.md", root=core_tree)
        assert verdict.in_core_tree is False

    def test_memory_reference_detection_is_case_insensitive(self, core_tree: Path) -> None:
        assert classify_destination("Memory/Topic.MD", root=core_tree).groundable is True

    def test_an_absolute_memory_path_is_groundable(self, core_tree: Path, tmp_path: Path) -> None:
        outside = tmp_path.parent / "claude-home" / "memory" / "topic.md"
        assert classify_destination(str(outside), root=core_tree).groundable is True

    def test_a_bare_memory_filename_with_no_parent_dir_is_not_groundable(self, core_tree: Path) -> None:
        # "memory.md" names no such per-project memory file — only the
        # parent-directory-qualified shape is recognised.
        verdict = classify_destination("memory.md", root=core_tree)
        assert verdict.is_memory_reference is False
        assert verdict.groundable is False

    def test_a_genuinely_unresolvable_destination_stays_withheld(self, core_tree: Path) -> None:
        # The required negative control: not every ungrounded destination is
        # rescued by the memory-reference carve-out.
        verdict = classify_destination("some/nonsense/path.md", root=core_tree)
        assert verdict.is_memory_reference is False
        assert verdict.groundable is False

    def test_a_real_core_tree_path_is_also_groundable(self, core_tree: Path) -> None:
        verdict = classify_destination("src/teatree/loops/dream/promote_memory.py", root=core_tree)
        assert verdict.in_core_tree is True
        assert verdict.groundable is True

    def test_a_memory_shaped_path_traversal_is_not_groundable(self, core_tree: Path) -> None:
        verdict = classify_destination("memory/../../etc/passwd.md", root=core_tree)
        assert verdict.is_memory_reference is False


class TestUnverifiableCheckoutFallsBackLoudly:
    """A site-packages install has no tree to read.

    Classifying everything as memory would silently kill the whole core-gap pipeline.
    """

    def test_the_legacy_prefix_predicate_decides_when_no_checkout_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(destination.PathHelpers, "core_repo_root", lambda **_: None)

        # `teatree/ghost.py` is the discriminating case: the prefix tuple matches it,
        # the real tree has no such path — so this goes RED if the fallback is skipped.
        assert points_at_core_fix("teatree/ghost.py") is True
        assert points_at_core_fix("feedback_tone.md") is False

    def test_the_verdict_reports_the_tree_as_unverifiable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(destination.PathHelpers, "core_repo_root", lambda **_: None)

        verdict = classify_destination("src/teatree/loops/dream/ghost_module.py")

        assert verdict.verifiable is False
        assert verdict.in_core_tree is True
