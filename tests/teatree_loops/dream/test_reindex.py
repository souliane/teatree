"""Phase 5 — regenerate the ``MEMORY.md`` index from the memory set (#1933 § 6).

Fixture-only: tmp dir, never the real ``~/.claude``. The contract: one line per
memory (clickable link + a ≤200-char summary), deduped, stably ordered, no
content moved into the index, and BYTE-IDENTICAL on a re-run with no changes.
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from teatree.loops.dream import reindex
from teatree.loops.dream.reindex import reindex_memory, render_index


class ReindexTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, name: str, text: str) -> Path:
        path = self.dir / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_one_line_per_memory_with_summary(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: the lease guard rejects an empty owner\n---\n# H\nbody")
        self._write("mem_b", "---\nname: mem_b\n---\n# H\nthe first prose line is the summary")
        result = reindex_memory(self.dir)
        index = (self.dir / "MEMORY.md").read_text(encoding="utf-8")
        assert result.lines_indexed == 2
        # #2723: a single bare filename pointer, no `[name.md](name.md)` href duplication.
        assert "- mem_a.md — the lease guard rejects an empty owner" in index
        assert "- mem_b.md — the first prose line is the summary" in index

    def test_no_href_duplication(self) -> None:
        # #2723: the old `[name.md](name.md)` listed the filename TWICE per line,
        # inflating the index. The pointer is now a single bare filename.
        self._write("mem_a", "---\nname: mem_a\nsummary: a lesson\n---\nbody")
        index = render_index(self.dir)
        assert "(mem_a.md)" not in index
        assert "[mem_a.md]" not in index
        assert index.count("mem_a.md") == 1

    def test_summary_is_clipped_to_120_chars(self) -> None:
        # #2723: the summary clip is shortened to ~120 chars to keep lines tight.
        long = "x" * 500
        self._write("mem_long", f"---\nname: mem_long\nsummary: {long}\n---\nbody")
        index = render_index(self.dir)
        line = next(line for line in index.splitlines() if "mem_long.md" in line)
        summary = line.split(" — ", 1)[1]
        assert len(summary) <= reindex._SUMMARY_MAX_CHARS
        assert reindex._SUMMARY_MAX_CHARS <= 120
        assert summary.endswith("…")

    def test_line_max_chars_pinned_to_140_and_hot_lines_capped(self) -> None:
        # #2723: the hot per-line cap is 140 (was 160) so `- <name>.md — <summary>` fits
        # even with a long filename. The cold MEMORY_ARCHIVE.md is uncapped — that is
        # decay's concern, pinned in test_decay.py::...::test_cold_index_signature_is_uncapped.
        assert reindex._LINE_MAX_CHARS == 140
        self._write("mem_long", f"---\nname: mem_long\nsummary: {'z' * 400}\n---\nbody")
        index = render_index(self.dir)
        hot_lines = [line for line in index.splitlines() if line.startswith("- ")]
        assert hot_lines
        for line in hot_lines:
            assert len(line) <= 140

    def test_archive_index_is_not_re_indexed_into_the_hot_index(self) -> None:
        # #2723: MEMORY_ARCHIVE.md lives in the memory dir but must never appear as a
        # hot index line (it is the cold index, excluded exactly like MEMORY.md).
        self._write("mem_a", "---\nname: mem_a\nsummary: a\n---\nbody")
        (self.dir / "MEMORY_ARCHIVE.md").write_text("# cold\n- archived_x.md — a signature\n", encoding="utf-8")
        index = render_index(self.dir)
        assert "MEMORY_ARCHIVE.md" not in index
        assert "archived_x.md" not in index
        assert "mem_a.md" in index

    def test_whole_line_is_capped(self) -> None:
        # #2723: the WHOLE line (filename + summary) is capped, so a long filename
        # plus a long summary can never blow the per-line byte budget. The pointer
        # filename is preserved intact; only the summary absorbs the cap.
        self._write("mem_with_a_very_long_descriptive_filename", f"---\nsummary: {'y' * 500}\n---\nbody")
        index = render_index(self.dir)
        line = next(line for line in index.splitlines() if line.startswith("- "))
        assert len(line) <= reindex._LINE_MAX_CHARS
        assert "mem_with_a_very_long_descriptive_filename.md" in line

    def test_filename_alone_over_budget_drops_the_summary(self) -> None:
        # #2723 edge: when the bare pointer filename ALONE exceeds the per-line budget
        # there is no room for any summary, so the line degrades to just the pointer
        # rather than emitting a negative-length clip.
        long_name = Path("m" * (reindex._LINE_MAX_CHARS + 10) + ".md")
        line = reindex._index_line(long_name, "a summary that cannot fit")
        assert line == f"- {long_name.name}"

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
        # The index carries the one-line summary, not the repeated body.
        assert index.count(body) == 0
        assert "short summary" in index

    def test_dry_run_writes_nothing(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: a\n---\nbody")
        result = reindex_memory(self.dir, dry_run=True)
        assert result.changed is True
        assert not (self.dir / "MEMORY.md").exists()

    def test_missing_dir_is_noop(self) -> None:
        result = reindex_memory(self.dir / "absent")
        assert result.lines_indexed == 0
        assert result.changed is False

    def test_unterminated_frontmatter_falls_back_to_body(self) -> None:
        # A "---" with no closing fence is not real frontmatter; the whole text is
        # treated as body and the first non-heading prose line becomes the summary
        # (the bare "---" line strips to empty and is skipped).
        self._write("mem_a", "---\nname: mem_a\nthe lesson with no closing fence")
        index = render_index(self.dir)
        line = next(line for line in index.splitlines() if "mem_a.md" in line)
        assert " — " in line  # a summary was derived from the body, not empty

    def test_memory_with_only_headings_has_no_summary(self) -> None:
        self._write("mem_a", "---\nname: mem_a\n---\n# Only A Heading\n## And Another")
        index = render_index(self.dir)
        # No prose line -> pointer only, no " — summary".
        assert "- mem_a.md\n" in index

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
        # Unlike the hot index line, the signature is never clipped.
        long = "a long lesson " + "x" * 400
        text = f"---\nname: m\ndescription: {long}\n---\nbody"
        assert reindex.signature_text(text) == long
        assert len(reindex.signature_text(text)) > reindex._LINE_MAX_CHARS

    def test_returned_signature_is_a_substring_of_the_text(self) -> None:
        # The retention contract: the signature stays findable in the body.
        sig = reindex.signature_text(self._NODE_TYPED)
        assert " ".join(sig.split()).lower() in " ".join(self._NODE_TYPED.split()).lower()

    def test_summary_for_shares_signature_text_then_clips(self) -> None:
        # The hot index summary is signature_text clipped to the per-summary cap.
        long = "y" * 400
        text = f"---\nname: m\ndescription: {long}\n---\nbody"
        summary = reindex._summary_for(text)
        assert len(summary) <= reindex._SUMMARY_MAX_CHARS
        assert summary.endswith("…")
        # The cold signature stays uncapped for the same text.
        assert len(reindex.signature_text(text)) > reindex._SUMMARY_MAX_CHARS
