"""Reclaimable control-DB copies become ONE question, never a deletion (A8).

`find_control_db_artifacts` names every file that is, or once was, a control database —
measured on this box: 40 files, 489 MB, two of them 71 MB copies of each other. It had one
consumer, and that consumer only asks who is HOLDING them open. Nothing ever proposed
reclaiming the space, so it accumulated silently.

A8 is explicit about the shape: the factory PROPOSES and the owner decides. The scanner
therefore files a question listing what it found and deletes nothing, ever — B8's default
is KEEP, and a database copy is exactly the artifact where a wrong delete is unrecoverable.
"""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.stale_control_db_questions import MARKER, StaleControlDbQuestionScanner


def _artifacts(tmp: Path, sizes: dict[str, int]) -> list[Path]:
    made = []
    for name, size in sizes.items():
        path = tmp / name
        path.write_bytes(b"x" * size)
        made.append(path)
    return made


class TestTheAskListsWhatItFoundAndDeletesNothing(TestCase):
    def setUp(self) -> None:
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def _scanner(self, sizes: dict[str, int]) -> StaleControlDbQuestionScanner:
        found = _artifacts(self.tmp, sizes)
        return StaleControlDbQuestionScanner(artifacts=lambda: found)

    def test_it_files_one_question_naming_the_files_and_the_space(self) -> None:
        signals = self._scanner({"db.sqlite3.precorrupt-1": 2048, "db.sqlite3-wal": 1024}).scan()

        row = DeferredQuestion.objects.get()
        assert row.dedupe_marker == MARKER
        assert "db.sqlite3.precorrupt-1" in row.question
        assert "2 file" in row.question
        assert len(signals) == 1

    def test_every_named_file_still_exists_afterwards(self) -> None:
        scanner = self._scanner({"db.sqlite3.precorrupt-1": 16})
        scanner.scan()
        assert all(path.exists() for path in scanner.artifacts()), "it reclaimed space nobody approved"

    def test_a_second_pass_files_nothing(self) -> None:
        scanner = self._scanner({"db.sqlite3.precorrupt-1": 16})
        scanner.scan()
        scanner.scan()
        assert DeferredQuestion.objects.count() == 1

    def test_a_box_with_nothing_to_reclaim_asks_nothing(self) -> None:
        assert StaleControlDbQuestionScanner(artifacts=list).scan() == []
        assert DeferredQuestion.objects.count() == 0

    def test_a_failing_probe_files_nothing_rather_than_crashing_the_tick(self) -> None:
        def boom() -> list[Path]:
            msg = "the data dir cannot be walked"
            raise OSError(msg)

        assert StaleControlDbQuestionScanner(artifacts=boom).scan() == []
