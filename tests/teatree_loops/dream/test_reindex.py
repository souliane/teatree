"""Phase 5 — regenerate the ``MEMORY.md`` index from the memory set (#1933 § 6).

Fixture-only: tmp dir, never the real ``~/.claude``. The contract: one line per
memory (clickable link + a ≤200-char summary), deduped, stably ordered, no
content moved into the index, and BYTE-IDENTICAL on a re-run with no changes.
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

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
        assert "- [mem_a.md](mem_a.md) — the lease guard rejects an empty owner" in index
        assert "- [mem_b.md](mem_b.md) — the first prose line is the summary" in index

    def test_summary_is_clipped_to_200_chars(self) -> None:
        long = "x" * 500
        self._write("mem_long", f"---\nname: mem_long\nsummary: {long}\n---\nbody")
        index = render_index(self.dir)
        line = next(line for line in index.splitlines() if "mem_long.md" in line)
        # link + " — " + ≤200-char (199 + ellipsis) summary
        summary = line.split(" — ", 1)[1]
        assert len(summary) <= 200
        assert summary.endswith("…")

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
        assert lines[0].startswith("- [alpha.md]")
        assert lines[1].startswith("- [zeta.md]")

    def test_index_does_not_index_itself(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: a\n---\nbody")
        (self.dir / "MEMORY.md").write_text("stale index\n", encoding="utf-8")
        index = render_index(self.dir)
        assert "MEMORY.md](MEMORY.md)" not in index

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
        # No prose line -> link only, no " — summary".
        assert "- [mem_a.md](mem_a.md)\n" in index

    def test_empty_dir_renders_header_only(self) -> None:
        index = render_index(self.dir)
        assert index.startswith("# Auto Memory")
        assert "- [" not in index

    def test_unreadable_file_is_skipped(self) -> None:
        self._write("mem_a", "---\nname: mem_a\nsummary: ok\n---\nbody")
        (self.dir / "broken.md").mkdir()
        index = render_index(self.dir)
        assert "mem_a.md" in index
        assert "broken.md" not in index
