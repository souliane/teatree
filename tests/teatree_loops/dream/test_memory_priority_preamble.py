"""``MEMORY_PRIORITY.md`` — the human-owned preamble the generated index cannot eat.

Two owners want the same file. Phase 5 regenerates ``MEMORY.md`` wholesale from the
memory set, and a reader hand-curates a "read first" block at its top carrying the
standing rules that cost the most when missed. One path means the next pass silently
deletes the curated block, and nothing goes red.

Separate paths resolve it: the pointer list stays generated (a hand-maintained list of
N filenames is a guaranteed drift for ~1 KB), the priority block stays hand-owned in
its own file, and the render concatenates them. The consequence this pins is the one
that is easy to lose: the curated block cites its memories in prose, so those stay
referenced — while every memory carried ONLY by its generated ``- name.md`` pointer
becomes eligible for the stale tier again, which is the behaviour a fully hand-curated
index had suppressed across the whole corpus.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.test import SimpleTestCase

from teatree.loops.dream import cross_link, gates
from teatree.loops.dream.decay import DecayPolicy, decay_memories
from teatree.loops.dream.reindex import PRIORITY_NAME, reindex_memory, render_index

_PREAMBLE = "# Auto Memory — Index\n\n## ⚠ Read first — highest-cost failures\n\n- **mem_a.md** — never do the thing\n"


class PriorityPreambleTestCase(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def _priority(self, text: str = _PREAMBLE) -> Path:
        return self._write(PRIORITY_NAME, text)

    def test_the_preamble_is_emitted_verbatim_above_the_pointers(self) -> None:
        self._write("mem_a.md", "---\nname: mem_a\n---\nbody")
        self._priority()

        index = render_index(self.dir)

        assert index.startswith(_PREAMBLE)
        assert "- mem_a.md\n" in index

    def test_the_preamble_file_is_not_itself_a_pointer(self) -> None:
        self._write("mem_a.md", "---\nname: mem_a\n---\nbody")
        self._priority()

        assert f"- {PRIORITY_NAME}" not in render_index(self.dir)

    def test_no_preamble_renders_the_plain_generated_index(self) -> None:
        self._write("mem_a.md", "---\nname: mem_a\n---\nbody")

        assert render_index(self.dir).startswith("# Auto Memory — Index")

    def test_a_re_run_is_byte_identical_and_leaves_the_preamble_alone(self) -> None:
        self._write("mem_a.md", "---\nname: mem_a\n---\nbody")
        priority = self._priority()

        reindex_memory(self.dir)
        first = (self.dir / "MEMORY.md").read_bytes()
        second_result = reindex_memory(self.dir)

        assert second_result.changed is False
        assert (self.dir / "MEMORY.md").read_bytes() == first
        assert priority.read_text(encoding="utf-8") == _PREAMBLE

    def test_an_edited_preamble_reaches_the_next_render(self) -> None:
        self._write("mem_a.md", "---\nname: mem_a\n---\nbody")
        self._priority()
        reindex_memory(self.dir)

        self._priority("# Auto Memory — Index\n\n## ⚠ Read first\n\n- **mem_a.md** — the rewritten rule\n")
        result = reindex_memory(self.dir)

        assert result.changed is True
        assert "the rewritten rule" in (self.dir / "MEMORY.md").read_text(encoding="utf-8")


class PreambleIsNotAMemoryTestCase(SimpleTestCase):
    """Every phase that walks ``*.md`` must treat the preamble as an index, not a lesson."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.dir / PRIORITY_NAME).write_text(_PREAMBLE, encoding="utf-8")

    def test_decay_never_archives_it(self) -> None:
        ancient = datetime.now(tz=UTC) + timedelta(days=3650)

        result = decay_memories(
            self.dir,
            now=ancient,
            has_durable_home=lambda _memory: True,
            policy=DecayPolicy(),
        )

        assert result.archived == ()
        assert (self.dir / PRIORITY_NAME).is_file()

    def test_cross_link_never_links_it(self) -> None:
        cross_link.cross_link_memories(self.dir)

        assert (self.dir / PRIORITY_NAME).read_text(encoding="utf-8") == _PREAMBLE

    def test_the_gates_snapshot_does_not_count_it_as_a_memory(self) -> None:
        snapshot = gates.snapshot_memory_dir(self.dir)

        assert PRIORITY_NAME not in snapshot.memories


class GeneratedPointerDoesNotProtectFromDecayTestCase(SimpleTestCase):
    """The consequence of generating the pointer list: the stale tier works again.

    A hand-curated index cites every memory in prose, so ``_is_referenced`` is true for
    the whole corpus and the stale tier can never fire. Under a generated index the lone
    ``- name.md`` pointer is excluded from citation counting — dropping that exclusion
    would strand the tier permanently, so both halves are pinned here.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ancient = datetime.now(tz=UTC) + timedelta(days=3650)

    def _decay(self) -> tuple[str, ...]:
        reindex_memory(self.dir)
        result = decay_memories(
            self.dir,
            now=self.ancient,
            has_durable_home=lambda _memory: True,
            policy=DecayPolicy(),
        )
        return tuple(archived.name for archived in result.archived)

    def test_a_memory_carried_only_by_its_generated_pointer_is_still_stale(self) -> None:
        (self.dir / "mem_lonely.md").write_text("---\nname: mem_lonely\n---\nbody", encoding="utf-8")

        assert "mem_lonely" in self._decay()

    def test_a_memory_cited_by_a_real_wikilink_is_retained(self) -> None:
        (self.dir / "mem_lonely.md").write_text("---\nname: mem_lonely\n---\nbody", encoding="utf-8")
        (self.dir / "mem_citer.md").write_text(
            "---\nname: mem_citer\n---\nsee [[mem_lonely]] for the rule",
            encoding="utf-8",
        )

        assert "mem_lonely" not in self._decay()
