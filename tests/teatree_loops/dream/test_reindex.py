"""Phase 5 — regenerate the ``MEMORY.md`` index from the memory set (#1933 § 6).

Fixture-only: tmp dir, never the real ``~/.claude``. The contract: one bare
``- name.md`` pointer per memory (no free-text summary, so the whole index stays
under the ~24 KB byte budget at a realistic corpus, #2755), deduped, stably
ordered, no content moved into the index, and BYTE-IDENTICAL on a re-run with no
changes.
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from teatree.loops.dream import gates, reindex
from teatree.loops.dream.reindex import index_line_for, reindex_memory, render_index


class ReindexTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, name: str, text: str) -> Path:
        path = self.dir / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_index_line_for_is_a_bare_basename_pointer(self) -> None:
        # the decay phase projects the post-archival index byte-for-byte via this helper
        assert index_line_for("mem_a") == "- mem_a"
        assert index_line_for("nested/dir/mem_b.md") == "- mem_b.md"

    def test_one_bare_pointer_per_memory(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: the lease guard rejects an empty owner\n---\n# H\nbody")
        self._write("mem_b", "---\nname: mem_b\n---\n# H\nthe first prose line is the summary")
        result = reindex_memory(self.dir)
        index = (self.dir / "MEMORY.md").read_text(encoding="utf-8")
        assert result.lines_indexed == 2
        # #2755: a bare filename pointer, no free-text summary — the slug is the hook.
        assert "- mem_a.md\n" in index
        assert "- mem_b.md\n" in index
        assert all(" — " not in line for line in index.splitlines() if line.startswith("- "))

    def test_no_href_duplication(self) -> None:
        # #2723/#2755: no `[name.md](name.md)` href duplication and no summary — the
        # pointer is a single bare filename mentioned exactly once.
        self._write("mem_a", "---\nname: mem_a\nsummary: a lesson\n---\nbody")
        index = render_index(self.dir)
        assert "(mem_a.md)" not in index
        assert "[mem_a.md]" not in index
        assert index.count("mem_a.md") == 1

    def test_archive_index_is_not_re_indexed_into_the_hot_index(self) -> None:
        # #2723: MEMORY_ARCHIVE.md lives in the memory dir but must never appear as a
        # hot index line (it is the cold index, excluded exactly like MEMORY.md).
        self._write("mem_a", "---\nname: mem_a\nsummary: a\n---\nbody")
        (self.dir / "MEMORY_ARCHIVE.md").write_text("# cold\n- archived_x.md — a signature\n", encoding="utf-8")
        index = render_index(self.dir)
        assert "MEMORY_ARCHIVE.md" not in index
        assert "archived_x.md" not in index
        assert "mem_a.md" in index

    def test_long_filename_renders_as_the_bare_pointer(self) -> None:
        # A long descriptive filename is preserved intact as the pointer — there is no
        # per-line summary to cap, so the whole line is just `- <name>.md`.
        self._write("mem_with_a_very_long_descriptive_filename", "---\nsummary: irrelevant\n---\nbody")
        index = render_index(self.dir)
        line = next(line for line in index.splitlines() if line.startswith("- "))
        assert line == "- mem_with_a_very_long_descriptive_filename.md"

    def test_idempotent_rerun_is_byte_identical(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: a\n---\nbody")
        self._write("mem_b", "---\nname: mem_b\nsummary: b\n---\nbody")
        first = reindex_memory(self.dir)
        assert first.changed is True
        before = (self.dir / "MEMORY.md").read_text(encoding="utf-8")
        second = reindex_memory(self.dir)
        assert second.changed is False
        assert (self.dir / "MEMORY.md").read_text(encoding="utf-8") == before

    def test_stable_ordering_by_filename(self) -> None:
        self._write("zeta", "---\nname: zeta\nsummary: z\n---\nbody")
        self._write("alpha", "---\nname: alpha\nsummary: a\n---\nbody")
        index = render_index(self.dir)
        lines = [line for line in index.splitlines() if line.startswith("- ")]
        assert lines[0].startswith("- alpha.md")
        assert lines[1].startswith("- zeta.md")

    def test_index_does_not_index_itself(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: a\n---\nbody")
        (self.dir / "MEMORY.md").write_text("stale index\n", encoding="utf-8")
        index = render_index(self.dir)
        assert "MEMORY.md" not in index

    def test_no_body_content_moved_into_index(self) -> None:
        body = "this is a long multi-line body that must NOT be copied into the index wholesale"
        self._write("mem_a", f"---\nname: mem_a\nsummary: short summary\n---\n{body}\n{body}\n{body}")
        index = render_index(self.dir)
        # The index carries only the bare pointer, never body content or the summary.
        assert index.count(body) == 0
        assert "short summary" not in index
        assert "- mem_a.md\n" in index

    def test_dry_run_writes_nothing(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: a\n---\nbody")
        result = reindex_memory(self.dir, dry_run=True)
        assert result.changed is True
        assert not (self.dir / "MEMORY.md").exists()

    def test_missing_dir_is_noop(self) -> None:
        result = reindex_memory(self.dir / "absent")
        assert result.lines_indexed == 0
        assert result.changed is False

    def test_every_memory_renders_as_a_bare_pointer_regardless_of_body(self) -> None:
        # The line is the bare pointer whether the body has prose, only headings, or an
        # unterminated frontmatter fence — the body never leaks into the hot index.
        self._write("mem_prose", "---\nname: mem_prose\nthe lesson with no closing fence")
        self._write("mem_headings", "---\nname: mem_headings\n---\n# Only A Heading\n## And Another")
        index = render_index(self.dir)
        assert "- mem_prose.md\n" in index
        assert "- mem_headings.md\n" in index
        assert all(" — " not in line for line in index.splitlines() if line.startswith("- "))

    def test_empty_dir_renders_header_only(self) -> None:
        index = render_index(self.dir)
        assert index.startswith("# Auto Memory")
        assert not any(line.startswith("- ") for line in index.splitlines())

    def test_unreadable_file_is_skipped(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: ok\n---\nbody")
        (self.dir / "broken.md").mkdir()
        index = render_index(self.dir)
        assert "mem_a.md" in index
        assert "broken.md" not in index

    def test_realistic_corpus_stays_under_budget(self) -> None:
        # #2755: the re-index must keep the WHOLE index under the ~24 KB
        # session-load byte budget at a realistic corpus. The old
        # ``- name.md — summary`` form rendered ~228 memories with ~50-byte filenames
        # OVER budget (undoing curated compaction); the bare ``- name.md`` pointer form
        # fits every pointer with room to spare. Anti-vacuous: this fixture renders
        # ~25 KB under the old summary generator (RED against both thresholds) and
        # ~14 KB under the bare-pointer one (GREEN), and every memory keeps one pointer.
        count = 228
        names = []
        for i in range(count):
            stem = f"feedback_realistic_descriptive_memory_lesson_slug_{i:04d}"
            names.append(f"{stem}.md")
            self._write(
                stem,
                f"---\nname: {stem}\n"
                f"summary: a recurring lesson about subsystem {i} the agent keeps relearning across sessions\n"
                f"---\nthe load-bearing body for lesson {i}\n",
            )
        index = render_index(self.dir)
        size = len(index.encode("utf-8"))
        assert size < 17_000, f"index is {size} bytes; must stay under the ~17 KB curated target"
        assert size < gates.INDEX_BYTE_BUDGET
        pointer_lines = [line for line in index.splitlines() if line.startswith("- ")]
        assert len(pointer_lines) == count  # one pointer line per memory
        assert all(index.count(name) == 1 for name in names)  # every file linked exactly once


class SignatureTextTestCase(SimpleTestCase):
    """The shared frontmatter-aware signature extractor (#2746 nit-4).

    ``signature_text`` is the ONE extractor the hot index, the cold
    ``MEMORY_ARCHIVE.md`` index, and the retention probe all share. It prefers
    the frontmatter ``description:``/``summary:`` over a node-type body line, so a
    node-typed memory no longer yields the near-vacuous ``node_type: memory``
    signature.
    """

    _NODE_TYPED = (
        "---\nname: feedback_example\n"
        "description: the lease guard rejects an empty owner address\n"
        "metadata:\n  type: feedback\n---\n"
        "node_type: memory\nsome trailing body content\n"
    )

    def test_frontmatter_description_preferred_over_node_type_body(self) -> None:
        # The bug: the old scanner returned the body ``node_type: memory`` line.
        assert reindex.signature_text(self._NODE_TYPED) == "the lease guard rejects an empty owner address"
        assert "node_type" not in reindex.signature_text(self._NODE_TYPED)

    def test_frontmatter_summary_preferred(self) -> None:
        text = "---\nname: m\nsummary: a tight one-line lesson\n---\n# H\nbody"
        assert reindex.signature_text(text) == "a tight one-line lesson"

    def test_body_node_type_line_is_skipped_when_no_frontmatter_summary(self) -> None:
        # No frontmatter description: the metadata ``node_type:`` line is skipped
        # and the next real lesson line is the signature.
        text = "---\nname: m\n---\nnode_type: memory\nthe actual recorded lesson\n"
        assert reindex.signature_text(text) == "the actual recorded lesson"

    def test_loose_metadata_lines_are_skipped(self) -> None:
        # A file with no fenced frontmatter, leading metadata-ish key lines.
        text = "name: mem_a\ntype: feedback\nthe load-bearing lesson A\n"
        assert reindex.signature_text(text) == "the load-bearing lesson A"

    def test_binding_heading_is_the_last_resort_signature(self) -> None:
        # No frontmatter, no prose — only headings, one declaring a BINDING rule.
        text = "---\nname: m\n---\n# Context\n## Non-Negotiable: never force-push main\n"
        assert reindex.signature_text(text) == "Non-Negotiable: never force-push main"

    def test_empty_when_no_signature_anywhere(self) -> None:
        text = "---\nname: m\n---\n# Only A Heading\n## And Another\n"
        assert reindex.signature_text(text) == ""

    def test_signature_is_uncapped(self) -> None:
        # The cold signature (retention + cold index) is never clipped, unlike the
        # bare-pointer hot index which carries no signature at all.
        long = "a long lesson " + "x" * 400
        text = f"---\nname: m\ndescription: {long}\n---\nbody"
        assert reindex.signature_text(text) == long
        assert len(reindex.signature_text(text)) > 200

    def test_returned_signature_is_a_substring_of_the_text(self) -> None:
        # The retention contract: the signature stays findable in the body.
        sig = reindex.signature_text(self._NODE_TYPED)
        assert " ".join(sig.split()).lower() in " ".join(self._NODE_TYPED.split()).lower()
