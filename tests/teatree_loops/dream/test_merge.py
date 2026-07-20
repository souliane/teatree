"""Phase 4b — merge near-duplicate memory files (#2723, the missing MERGE verb).

Fixture-only: every test writes ``*.md`` into a tmp dir and never touches the
real ``~/.claude``. The contract:

*   two NEAR-DUPLICATE files (Jaccard >= the HIGH floor AND same ``type``/``name``
    family) collapse into ONE survivor — the higher-weight file keeps its content
    and absorbs the other's distinct lines; the absorbed file is ARCHIVED (moved
    with a provenance header), never deleted, and no lesson is lost;
*   two merely-RELATED files (related enough to cross-link but below the near-dup
    floor) are NOT merged;
*   a BINDING rule always SURVIVES a merge — the higher-weight survivor keeps the
    binding doctrine;
*   two CONFLICTING BINDING files are NEVER auto-merged — they are cross-linked and
    surfaced as a reconciliation conflict (the ticket path), so binding doctrine is
    never silently collapsed.

The merge phase is PURE and idempotent: a re-run on a set with no near-duplicates
adds nothing. It is fault-isolated by the command's try/except.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import SimpleTestCase

from teatree.loops.dream import merge
from teatree.loops.dream.merge import merge_memories

# A long, highly-overlapping topic body so two copies clear the 0.85 near-dup floor.
_TOPIC = (
    "the worktree provision lease pid-anchored claim guard rejects an empty owner "
    "liveness probe session heartbeat expiry compare-and-swap concurrent acquire "
    "release reaper stale lease ttl budget seconds owner token isolation"
)


class MergeTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, name: str, body: str, *, frontmatter: str = "") -> Path:
        path = self.dir / f"{name}.md"
        front = f"---\nname: {name}\n{frontmatter}---\n"
        path.write_text(f"{front}{body}\n", encoding="utf-8")
        return path

    def test_near_duplicate_files_collapse_to_one_no_lesson_lost(self) -> None:
        a = self._write("feedback_dup_a", _TOPIC + " and the FIRST distinct detail", frontmatter="type: feedback\n")
        b = self._write("feedback_dup_b", _TOPIC + " and the SECOND distinct detail", frontmatter="type: feedback\n")
        result = merge_memories(self.dir)

        assert result.merged_count == 1
        # One survivor remains; the other is archived (moved, not deleted).
        survivors = {p.name for p in self.dir.glob("*.md") if p.name != "MEMORY.md"}
        assert len(survivors) == 1
        survivor_name = next(iter(survivors))
        survivor_text = (self.dir / survivor_name).read_text(encoding="utf-8")
        # No lesson lost: BOTH distinct details survive in the merged file.
        assert "FIRST distinct detail" in survivor_text
        assert "SECOND distinct detail" in survivor_text
        # The absorbed file is archived with a provenance header, never deleted.
        absorbed = "feedback_dup_b" if survivor_name == "feedback_dup_a.md" else "feedback_dup_a"
        archived = self.dir / "archive" / f"{absorbed}.md"
        assert archived.is_file()
        assert "merged into" in archived.read_text(encoding="utf-8")
        assert not (a if absorbed == "feedback_dup_a" else b).exists()

    def test_distinct_lines_excludes_the_absorbed_frontmatter(self) -> None:
        # F6.8: the absorbed file's own frontmatter (name:/type:/---) must NOT be
        # carried into the survivor body — appending `name: <absorbed>` would let the
        # survivor NAME resolve to the absorbed slug and skew refcount/score. Only the
        # absorbed LESSON body is distinct-diffed into the survivor.
        from datetime import UTC, datetime  # noqa: PLC0415

        from teatree.loops.dream.decay import _MemoryFile  # noqa: PLC0415

        now = datetime.now(tz=UTC)
        survivor = _MemoryFile(
            path=self.dir / "s.md", name="s", text="---\nname: s\ntype: feedback\n---\nsurvivor body\n", mtime=now
        )
        absorbed = _MemoryFile(
            path=self.dir / "a.md",
            name="a",
            text="---\nname: absorbed_slug\ntype: feedback\n---\nunique absorbed lesson line\n",
            mtime=now,
        )
        lines = merge._distinct_lines(survivor, absorbed)
        assert "unique absorbed lesson line" in lines
        assert not any("name: absorbed_slug" in line for line in lines)
        assert not any(line.strip() == "---" for line in lines)

    def test_survivor_name_does_not_resolve_to_the_absorbed_slug_after_merge(self) -> None:
        # F6.8 end-to-end: after a real merge the survivor still resolves to its OWN
        # name, never the absorbed file's slug appended into its body.
        from teatree.loops.dream.decay import _memory_name  # noqa: PLC0415

        self._write("feedback_dup_a", _TOPIC + " and the FIRST distinct detail", frontmatter="type: feedback\n")
        self._write("feedback_dup_b", _TOPIC + " and the SECOND distinct detail", frontmatter="type: feedback\n")
        merge_memories(self.dir)
        survivors = {p.name for p in self.dir.glob("*.md") if p.name != "MEMORY.md"}
        survivor_name = next(iter(survivors))
        text = (self.dir / survivor_name).read_text(encoding="utf-8")
        absorbed_stem = "feedback_dup_b" if survivor_name == "feedback_dup_a.md" else "feedback_dup_a"
        assert f"name: {absorbed_stem}" not in text
        assert _memory_name(self.dir / survivor_name, text) == survivor_name.removesuffix(".md")

    def test_merely_related_files_are_not_merged(self) -> None:
        # Related enough to cross-link (some shared tokens) but below the near-dup
        # floor -> NOT merged.
        self._write("feedback_a", _TOPIC, frontmatter="type: feedback\n")
        self._write(
            "feedback_b",
            "worktree provision lease guard but otherwise a completely different "
            "subject about slack notify thread timestamp channel speak tts digest receipt",
            frontmatter="type: feedback\n",
        )
        result = merge_memories(self.dir)
        assert result.merged_count == 0
        survivors = {p.name for p in self.dir.glob("*.md") if p.name != "MEMORY.md"}
        assert survivors == {"feedback_a.md", "feedback_b.md"}

    def test_binding_rule_survives_a_merge(self) -> None:
        # The binding file is higher-weight, so it is the survivor and keeps its
        # binding doctrine; the non-binding near-duplicate is absorbed. The bodies
        # are near-identical (same topic), the only marker being BINDING.
        self._write(
            "feedback_binding",
            _TOPIC + " BINDING marker line",
            frontmatter="type: feedback\n",
        )
        self._write("feedback_plain", _TOPIC + " plain marker line", frontmatter="type: feedback\n")
        result = merge_memories(self.dir)
        assert result.merged_count == 1
        # The survivor is the binding file; its doctrine is intact.
        survivor = self.dir / "feedback_binding.md"
        assert survivor.is_file()
        assert "BINDING marker line" in survivor.read_text(encoding="utf-8")
        assert not (self.dir / "feedback_plain.md").exists()

    def test_two_conflicting_binding_files_are_not_merged_ticket_path(self) -> None:
        # Decision-3: two BINDING near-duplicates are NEVER auto-merged. They are
        # cross-linked and surfaced as a reconciliation conflict (the ticket path).
        a = self._write(
            "feedback_bind_one",
            _TOPIC + " BINDING: always do X before the push",
            frontmatter="type: feedback\n",
        )
        b = self._write(
            "feedback_bind_two",
            _TOPIC + " BINDING: never do X before the push",
            frontmatter="type: feedback\n",
        )
        result = merge_memories(self.dir)
        assert result.merged_count == 0
        # Both BINDING files survive untouched on disk.
        assert a.exists()
        assert b.exists()
        # The conflict is surfaced for human reconciliation (the ticket path).
        assert len(result.binding_conflicts) == 1
        conflict = result.binding_conflicts[0]
        assert {conflict.survivor_name, conflict.absorbed_name} == {"feedback_bind_one", "feedback_bind_two"}
        # Cross-linked so a reader landing on one finds the other.
        assert "[[feedback_bind_two]]" in a.read_text(encoding="utf-8")
        assert "[[feedback_bind_one]]" in b.read_text(encoding="utf-8")

    def test_absorbed_is_archived_before_survivor_rewrite_so_a_crash_never_leaves_both(self) -> None:
        # Archive-first crash-safety: if the survivor rewrite dies mid-merge, the
        # absorbed file must already be archived (gone from the live dir) so a re-run
        # never re-pairs both live files and re-applies the merge. The old order
        # (rewrite survivor, then archive) left BOTH files live on a kill.
        self._write("feedback_dup_a", _TOPIC + " FIRST", frontmatter="type: feedback\n")
        self._write("feedback_dup_b", _TOPIC + " SECOND", frontmatter="type: feedback\n")

        # Crash the survivor rewrite step (which runs AFTER the absorbed file is
        # archived) by raising from the provenance builder it depends on.
        with (
            patch.object(merge, "_merge_provenance", side_effect=OSError("survivor rewrite boom")),
            pytest.raises(OSError, match="boom"),
        ):
            merge_memories(self.dir)

        live = {p.name for p in self.dir.glob("feedback_dup_*.md")}
        assert len(live) == 1  # only the survivor is still live — the absorbed was archived first
        assert (self.dir / "archive").is_dir()

    def test_idempotent_rerun_merges_nothing(self) -> None:
        self._write("feedback_dup_a", _TOPIC + " FIRST", frontmatter="type: feedback\n")
        self._write("feedback_dup_b", _TOPIC + " SECOND", frontmatter="type: feedback\n")
        merge_memories(self.dir)
        second = merge_memories(self.dir)
        assert second.merged_count == 0

    def test_dry_run_merges_nothing_on_disk(self) -> None:
        a = self._write("feedback_dup_a", _TOPIC + " FIRST", frontmatter="type: feedback\n")
        b = self._write("feedback_dup_b", _TOPIC + " SECOND", frontmatter="type: feedback\n")
        before_a = a.read_text(encoding="utf-8")
        before_b = b.read_text(encoding="utf-8")
        result = merge_memories(self.dir, dry_run=True)
        assert result.merged_count == 1  # decision computed
        assert a.read_text(encoding="utf-8") == before_a  # nothing moved
        assert b.read_text(encoding="utf-8") == before_b
        assert not (self.dir / "archive").exists()

    def test_different_family_near_duplicates_are_not_merged(self) -> None:
        # Same topic tokens but DIFFERENT frontmatter type -> not the same family,
        # so they are not merged (a feedback rule and a reference note stay apart).
        self._write("feedback_x", _TOPIC, frontmatter="type: feedback\n")
        self._write("reference_x", _TOPIC, frontmatter="type: reference\n")
        result = merge_memories(self.dir)
        assert result.merged_count == 0

    def test_missing_dir_is_noop(self) -> None:
        result = merge_memories(self.dir / "absent")
        assert result.merged_count == 0
        assert result.seen == 0

    def test_jaccard_floor_is_high(self) -> None:
        # The near-dup floor is HIGH (>=0.85), far above the cross-link floor (0.18).
        assert merge._NEAR_DUPLICATE_FLOOR >= 0.85

    def test_family_falls_back_to_filename_stem_without_type(self) -> None:
        # No frontmatter `type:` -> the family is the filename's leading token, so
        # two `feedback_*` near-duplicates still collapse.
        a = self.dir / "feedback_no_type_a.md"
        b = self.dir / "feedback_no_type_b.md"
        a.write_text(f"{_TOPIC} FIRST\n", encoding="utf-8")
        b.write_text(f"{_TOPIC} SECOND\n", encoding="utf-8")
        result = merge_memories(self.dir)
        assert result.merged_count == 1

    def test_survivor_is_the_higher_weight_file_regardless_of_order(self) -> None:
        # The binding file sorts SECOND alphabetically but is higher-weight, so it
        # must still be the survivor — the weight, not the order, decides.
        self._write("aaa_plain", _TOPIC + " plain restatement", frontmatter="type: feedback\n")
        self._write("zzz_binding", _TOPIC + " BINDING marker", frontmatter="type: feedback\n")
        result = merge_memories(self.dir)
        assert result.merged_count == 1
        assert (self.dir / "zzz_binding.md").is_file()  # binding survived
        assert not (self.dir / "aaa_plain.md").exists()  # plain absorbed

    def test_three_near_duplicates_merge_one_disjoint_pair_per_pass(self) -> None:
        # Three near-duplicates: only ONE disjoint pair collapses per pass — the
        # survivor and absorbed of the first pair are both consumed, so the third
        # file has no available partner this pass (the disjoint/consumed guard).
        self._write("feedback_t1", _TOPIC + " ONE detail", frontmatter="type: feedback\n")
        self._write("feedback_t2", _TOPIC + " TWO detail", frontmatter="type: feedback\n")
        self._write("feedback_t3", _TOPIC + " THREE detail", frontmatter="type: feedback\n")
        first = merge_memories(self.dir)
        assert first.merged_count == 1
        survivors = {p.name for p in self.dir.glob("feedback_t*.md")}
        # Exactly one pair collapsed: two files remain (the first pair's survivor
        # plus the un-paired third).
        assert len(survivors) == 2

    def test_dry_run_reports_the_binding_conflict_but_writes_nothing(self) -> None:
        a = self._write("feedback_bind_one", _TOPIC + " BINDING: always", frontmatter="type: feedback\n")
        b = self._write("feedback_bind_two", _TOPIC + " BINDING: never", frontmatter="type: feedback\n")
        before_a = a.read_text(encoding="utf-8")
        result = merge_memories(self.dir, dry_run=True)
        # Dry-run PREVIEWS the binding conflict a real run would surface (never a
        # silent zero), while the cross-link side effect stays on disk-untouched.
        assert len(result.binding_conflicts) == 1
        assert {result.binding_conflicts[0].survivor_name, result.binding_conflicts[0].absorbed_name} == {
            "feedback_bind_one",
            "feedback_bind_two",
        }
        assert a.read_text(encoding="utf-8") == before_a
        assert "[[" not in b.read_text(encoding="utf-8")

    def test_retro_file_outranks_a_plain_other_file(self) -> None:
        # A retro-named file outranks an unweighted 'other' file, so the retro file
        # survives the merge.
        self._write("retro_finding", _TOPIC + " a retro lesson", frontmatter="type: retro\n")
        self._write("plain_note", _TOPIC + " a plain note", frontmatter="type: retro\n")
        result = merge_memories(self.dir)
        assert result.merged_count == 1
        assert (self.dir / "retro_finding.md").is_file()
        assert not (self.dir / "plain_note.md").exists()

    def test_two_disjoint_pairs_collapse_in_one_pass(self) -> None:
        # Four near-duplicates forming TWO disjoint pairs, INTERLEAVED by filename so
        # the first pair consumes a file the second pair's outer iteration scans —
        # exercising the consumed-skip on a later candidate. Both pairs collapse.
        unrelated = (
            "slack notify thread timestamp channel speak tts message digest receipt "
            "reaction emoji broadcast outcome reply discussion resolve update note egress"
        )
        # Sorted order: m_1, m_2, m_3, m_4. m_1~m_3 (topic A), m_2~m_4 (topic B).
        self._write("feedback_m_1", _TOPIC + " AONE", frontmatter="type: feedback\n")
        self._write("feedback_m_2", unrelated + " BONE", frontmatter="type: feedback\n")
        self._write("feedback_m_3", _TOPIC + " ATWO", frontmatter="type: feedback\n")
        self._write("feedback_m_4", unrelated + " BTWO", frontmatter="type: feedback\n")
        result = merge_memories(self.dir)
        assert result.merged_count == 2
        survivors = {p.name for p in self.dir.glob("feedback_m_*.md")}
        assert len(survivors) == 2

    def test_cross_link_is_idempotent_when_link_already_present(self) -> None:
        # A binding conflict whose files already carry the wikilink is not
        # double-linked on a re-run.
        self._write(
            "feedback_bind_one",
            _TOPIC + " BINDING: always\nRelated: [[feedback_bind_two]]",
            frontmatter="type: feedback\n",
        )
        self._write(
            "feedback_bind_two",
            _TOPIC + " BINDING: never\nRelated: [[feedback_bind_one]]",
            frontmatter="type: feedback\n",
        )
        merge_memories(self.dir)
        a = (self.dir / "feedback_bind_one.md").read_text(encoding="utf-8")
        assert a.count("[[feedback_bind_two]]") == 1
