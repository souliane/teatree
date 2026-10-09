"""Unit surface of the incompleteness-marker scanner.

The whole-tree ratchet built on this module lives in
``tests/quality/test_incompleteness_marker_ratchet.py``; this file exercises the
walk itself against fixtures: how a wrapped phrase is recovered, which issue
reference reads as a tracking pointer, and the registry's own validation.
"""

from pathlib import Path

import pytest

from teatree.quality.incompleteness_markers import (
    DeferredMarkerBan,
    MarkerForm,
    MarkerPattern,
    MarkerRegistryError,
    applicable_patterns,
    issue_deferrals,
    issue_refs_near,
    load_marker_patterns,
    per_file_counts,
    registry_path,
    scan_file,
    scan_tree,
    scanned_files,
)

PATTERNS = load_marker_patterns()

# A minimal well-formed entry; each malformed case below removes exactly one part.
_ENTRY = "markers:\n  - id: a\n    name: n\n    concern: c\n    remedy: r\n    pattern: x\n    triggers: [x]\n"


def _scan(tmp_path: Path, body: str, name: str = "mod.py") -> list[str]:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return [marker.pattern_id for marker in scan_file(path, PATTERNS, repo_root=tmp_path)]


class TestRegistry:
    def test_ships_families_with_a_remedy_each(self) -> None:
        assert PATTERNS
        assert all(pattern.remedy.strip() for pattern in PATTERNS)

    def test_ids_are_unique(self) -> None:
        assert len({pattern.id for pattern in PATTERNS}) == len(PATTERNS)

    def test_registry_file_sits_next_to_the_module(self) -> None:
        assert registry_path().is_file()

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ("markers: []", "non-empty 'markers'"),
            ("markers:\n  - [1, 2]", "must be a mapping"),
            (_ENTRY.replace("    concern: c\n", ""), "'concern'"),
            (_ENTRY + _ENTRY.removeprefix("markers:\n"), "duplicate marker id"),
            (_ENTRY.replace("    triggers: [x]\n", ""), "triggers"),
            (_ENTRY + "    form: sometimes\n", "'form' must be one of"),
        ],
    )
    def test_malformed_registry_is_rejected(self, tmp_path: Path, payload: str, message: str) -> None:
        broken = tmp_path / "registry.yaml"
        broken.write_text(payload, encoding="utf-8")
        with pytest.raises(MarkerRegistryError, match=message):
            load_marker_patterns(broken)


class TestMarkerForm:
    def test_a_family_describes_a_sentence_shape_unless_it_says_otherwise(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry.yaml"
        registry.write_text(_ENTRY, encoding="utf-8")
        assert load_marker_patterns(registry)[0].form is MarkerForm.PROSE

    def test_the_ban_scope_follows_the_registry_rather_than_a_second_list(self, tmp_path: Path) -> None:
        # Give a NEW family `form: marker` and the verification-tree ban covers
        # it, with nothing else to edit.
        registry = tmp_path / "registry.yaml"
        registry.write_text(
            "markers:\n  - id: wip\n    name: n\n    concern: c\n    remedy: r\n"
            "    pattern: 'WIP\\s*:'\n    triggers: [wip]\n    form: marker\n",
            encoding="utf-8",
        )
        patterns = load_marker_patterns(registry)
        assert [pattern.id for pattern in DeferredMarkerBan.over(tmp_path, patterns).patterns] == ["wip"]

        probe = tmp_path / "tests" / "test_probe.py"
        probe.parent.mkdir()
        probe.write_text("# WIP: half an assertion\n", encoding="utf-8")
        assert [marker.pattern_id for marker in DeferredMarkerBan.over(tmp_path, patterns).markers()] == ["wip"]


class TestWrappedPhrases:
    def test_a_phrase_split_across_docstring_lines_is_found(self, tmp_path: Path) -> None:
        assert _scan(tmp_path, '"""The tier is not\nwired into the resolver."""\n') == ["not-wired"]

    def test_a_phrase_split_across_comment_lines_is_found(self, tmp_path: Path) -> None:
        assert _scan(tmp_path, "# The tier is not\n# wired into the resolver.\nx = 1\n") == ["not-wired"]

    def test_a_phrase_is_counted_once_at_its_starting_line(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.py"
        path.write_text('"""Line one is not\nwired at all.\n\nAnd a tail line."""\n', encoding="utf-8")
        markers = scan_file(path, PATTERNS, repo_root=tmp_path)
        assert [(marker.lineno, marker.pattern_id) for marker in markers] == [(1, "not-wired")]


class TestCaseSensitivity:
    def test_shouted_admissions_are_found(self, tmp_path: Path) -> None:
        assert _scan(tmp_path, '"""The carve-out is retained but EMPTY."""\n') == ["retained-but-empty"]

    def test_a_marker_is_an_uppercase_convention(self, tmp_path: Path) -> None:
        assert _scan(tmp_path, '"""That was a hack: it worked anyway."""\n') == []
        assert _scan(tmp_path, '"""HACK: it worked anyway."""\n') == ["author-marker"]

    def test_the_id_namespace_is_not_a_marker(self, tmp_path: Path) -> None:
        assert _scan(tmp_path, '"""Rendered as TODO-7 in the statusline."""\n') == []


class TestIssueReferences:
    def test_a_nearby_reference_is_a_tracking_pointer(self) -> None:
        assert issue_refs_near("deferred to a follow-up (#2240)", (0, 22)) == (2240,)

    def test_a_distant_reference_is_a_citation(self) -> None:
        text = "deferred to a follow-up" + " padding" * 20 + " shipped under #2240"
        assert issue_refs_near(text, (0, 23)) == ()

    def test_deferrals_pair_each_marker_with_each_issue(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.md"
        path.write_text("remains future work under #724/#725.\n", encoding="utf-8")
        markers = scan_file(path, PATTERNS, repo_root=tmp_path)
        assert [deferral.issue for deferral in issue_deferrals(markers)] == [724, 725]

    def test_a_marker_with_no_reference_yields_no_deferral(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.md"
        path.write_text("remains future work.\n", encoding="utf-8")
        assert issue_deferrals(scan_file(path, PATTERNS, repo_root=tmp_path)) == []


class TestFailureText:
    """What a blocked author actually reads: file, line, phrase, and what to do."""

    def test_a_marker_names_its_location_phrase_and_remedy(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text('"""The tier is not wired in."""\n', encoding="utf-8")
        described = scan_file(tmp_path / "mod.py", PATTERNS, repo_root=tmp_path)[0].describe()
        assert "mod.py:1" in described
        assert "not wired" in described
        assert "Wire it" in described

    def test_a_deferral_names_its_tracking_issue(self, tmp_path: Path) -> None:
        (tmp_path / "doc.md").write_text("remains future work under #724.\n", encoding="utf-8")
        markers = scan_file(tmp_path / "doc.md", PATTERNS, repo_root=tmp_path)
        described = issue_deferrals(markers)[0].describe()
        assert "doc.md:1" in described
        assert "#724" in described


class TestPreFilter:
    def test_admits_only_families_whose_trigger_appears(self) -> None:
        admitted = applicable_patterns("the carve-out is retained but empty", PATTERNS)
        assert [pattern.id for pattern in admitted] == ["retained-but-empty"]

    def test_admits_nothing_for_unrelated_text(self) -> None:
        assert applicable_patterns("a plain sentence about rows and columns", PATTERNS) == ()

    def test_a_pattern_with_no_matching_trigger_is_skipped(self, tmp_path: Path) -> None:
        # The trigger contract is what makes the filter sound: a family whose
        # trigger is absent from the text can never match it.
        blind = MarkerPattern(
            id="blind", name="n", concern="c", remedy="r", regex=PATTERNS[0].regex, triggers=("zzzz",)
        )
        (tmp_path / "mod.py").write_text('"""TODO: something."""\n', encoding="utf-8")
        assert scan_tree(tmp_path, [blind]) == []


class TestScannedSet:
    def test_a_missing_root_is_not_an_error(self, tmp_path: Path) -> None:
        assert scanned_files(tmp_path) == []

    def test_markdown_outside_the_doc_roots_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.md").write_text("remains future work.\n", encoding="utf-8")
        assert scan_tree(tmp_path) == []

    def test_counts_group_markers_by_file(self, tmp_path: Path) -> None:
        module = tmp_path / "src" / "teatree" / "mod.py"
        module.parent.mkdir(parents=True)
        module.write_text('"""TODO: one."""\n\n# TODO: two\nx = 1\n', encoding="utf-8")
        assert per_file_counts(scan_tree(tmp_path)) == {"src/teatree/mod.py": 2}
