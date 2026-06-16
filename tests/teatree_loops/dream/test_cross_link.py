"""Phase 4 — cross-link related memories with ``[[name]]`` links (#1933 § 6).

Fixture-only: every test writes ``*.md`` into a tmp dir and never touches the
real ``~/.claude``. The contract: related memories (shared topic tokens above the
floor) get symmetric ``[[name]]`` links; unrelated ones do not; and a re-run on an
unchanged set adds nothing (idempotent).
"""

import tempfile
from pathlib import Path

import pytest
from django.test import SimpleTestCase

from teatree.loops.dream.cross_link import _jaccard, cross_link_memories

_TOPIC_A = "the worktree provision lease pid-anchored claim guard rejects an empty owner liveness"
_TOPIC_B = "worktree provision lease pid claim guard owner liveness anchored on the session"
_UNRELATED = "slack notify thread timestamp channel speak tts message digest receipt"


class CrossLinkTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, name: str, body: str) -> Path:
        path = self.dir / f"{name}.md"
        path.write_text(f"name: {name}\n{body}\n", encoding="utf-8")
        return path

    def test_related_memories_get_symmetric_links(self) -> None:
        a = self._write("mem_a", _TOPIC_A)
        b = self._write("mem_b", _TOPIC_B)
        result = cross_link_memories(self.dir)
        assert result.links_added == 2
        assert "[[mem_b]]" in a.read_text(encoding="utf-8")
        assert "[[mem_a]]" in b.read_text(encoding="utf-8")

    def test_unrelated_memory_is_not_linked(self) -> None:
        self._write("mem_a", _TOPIC_A)
        self._write("mem_b", _TOPIC_B)
        unrelated = self._write("mem_c", _UNRELATED)
        cross_link_memories(self.dir)
        text = unrelated.read_text(encoding="utf-8")
        assert "[[" not in text

    def test_idempotent_rerun_adds_no_links(self) -> None:
        self._write("mem_a", _TOPIC_A)
        self._write("mem_b", _TOPIC_B)
        cross_link_memories(self.dir)
        second = cross_link_memories(self.dir)
        assert second.links_added == 0

    def test_existing_link_is_not_duplicated(self) -> None:
        a = self._write("mem_a", _TOPIC_A + "\nRelated: [[mem_b]]")
        self._write("mem_b", _TOPIC_B)
        cross_link_memories(self.dir)
        assert a.read_text(encoding="utf-8").count("[[mem_b]]") == 1

    def test_dry_run_writes_nothing(self) -> None:
        a = self._write("mem_a", _TOPIC_A)
        self._write("mem_b", _TOPIC_B)
        before = a.read_text(encoding="utf-8")
        result = cross_link_memories(self.dir, dry_run=True)
        assert result.links_added == 2
        assert a.read_text(encoding="utf-8") == before

    def test_memory_index_is_excluded(self) -> None:
        self._write("mem_a", _TOPIC_A)
        self._write("mem_b", _TOPIC_B)
        (self.dir / "MEMORY.md").write_text("# index\n- [mem_a.md](mem_a.md)\n", encoding="utf-8")
        result = cross_link_memories(self.dir)
        # MEMORY.md is not a memory file, so files_seen counts only the two.
        assert result.files_seen == 2

    def test_missing_dir_is_noop(self) -> None:
        result = cross_link_memories(self.dir / "absent")
        assert result.files_seen == 0
        assert result.links_added == 0

    def test_name_falls_back_to_stem_without_frontmatter(self) -> None:
        a = self.dir / "stem_a.md"
        b = self.dir / "stem_b.md"
        a.write_text(_TOPIC_A + "\n", encoding="utf-8")
        b.write_text(_TOPIC_B + "\n", encoding="utf-8")
        cross_link_memories(self.dir)
        assert "[[stem_b]]" in a.read_text(encoding="utf-8")

    def test_unreadable_file_is_skipped_not_fatal(self) -> None:
        self._write("mem_a", _TOPIC_A)
        # A directory named like a memory file makes read_text raise OSError; the
        # loader skips it rather than crashing.
        (self.dir / "broken.md").mkdir()
        result = cross_link_memories(self.dir)
        assert result.files_seen == 1

    def test_single_memory_has_no_relation(self) -> None:
        # Only one memory: the inner relation loop never runs, no link added.
        self._write("solo", _TOPIC_A)
        result = cross_link_memories(self.dir)
        assert result.links_added == 0

    def test_disjoint_topics_are_not_linked(self) -> None:
        # No shared topic tokens -> Jaccard 0 -> not related.
        self._write("mem_a", _TOPIC_A)
        self._write("mem_c", _UNRELATED)
        result = cross_link_memories(self.dir)
        assert result.links_added == 0

    def test_token_less_memory_is_not_linked(self) -> None:
        # A memory whose words are all stopwords/too-short has an empty token set
        # -> Jaccard short-circuits to 0, never linked.
        self._write("mem_a", _TOPIC_A)
        self._write("mem_empty", "a an the to of in on at is it by")
        result = cross_link_memories(self.dir)
        assert result.links_added == 0

    def test_same_name_different_files_are_not_self_linked(self) -> None:
        # Two files sharing the same frontmatter name must not link each other
        # (a memory never links its own name).
        (self.dir / "copy1.md").write_text(f"name: dup\n{_TOPIC_A}\n", encoding="utf-8")
        (self.dir / "copy2.md").write_text(f"name: dup\n{_TOPIC_B}\n", encoding="utf-8")
        result = cross_link_memories(self.dir)
        assert result.links_added == 0


class JaccardTestCase(SimpleTestCase):
    """The pure topic-overlap ratio — the floor used to decide relatedness."""

    def test_empty_set_short_circuits_to_zero(self) -> None:
        # An empty token set on either side -> 0.0 (the `not a or not b` guard).
        assert not _jaccard(frozenset(), frozenset({"worktree", "lease"}))
        assert not _jaccard(frozenset({"worktree", "lease"}), frozenset())

    def test_disjoint_sets_short_circuit_to_zero(self) -> None:
        # Non-empty but no shared token -> intersection 0 -> 0.0 (the `inter == 0`
        # guard), without computing the union ratio.
        assert not _jaccard(frozenset({"worktree", "lease"}), frozenset({"slack", "notify"}))

    def test_overlap_is_intersection_over_union(self) -> None:
        # Two shared of three total -> 2/4 = 0.5.
        assert _jaccard(frozenset({"a", "b", "c"}), frozenset({"a", "b", "d"})) == pytest.approx(0.5)
