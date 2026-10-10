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
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.stale_control_db_questions import MARKER_PREFIX, StaleControlDbQuestionScanner
from tests._owner_channel import assert_a_plain_card, shown_text


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

    def test_it_files_one_card_with_the_count_and_the_space_but_no_file_name(self) -> None:
        signals = self._scanner({"db.sqlite3.precorrupt-1": 2048, "db.sqlite3-wal": 1024}).scan()

        row = DeferredQuestion.objects.get()
        text = assert_a_plain_card(row, "db.sqlite3", "precorrupt", "wal")
        assert row.dedupe_marker.startswith(MARKER_PREFIX)
        assert "2 files" in text
        assert "MiB" in text
        assert row.evidence["decision"] == "irreversible"
        assert len(signals) == 1

    def test_the_signal_still_names_what_was_found(self) -> None:
        (signal,) = self._scanner({"db.sqlite3.precorrupt-1": 2048, "db.sqlite3-wal": 1024}).scan()

        assert "db.sqlite3.precorrupt-1" in signal.summary
        assert "db.sqlite3-wal" in signal.summary

    def test_every_named_file_still_exists_afterwards(self) -> None:
        scanner = self._scanner({"db.sqlite3.precorrupt-1": 16})
        scanner.scan()
        assert all(path.exists() for path in scanner.artifacts()), "it reclaimed space nobody approved"

    def test_a_second_pass_files_nothing(self) -> None:
        scanner = self._scanner({"db.sqlite3.precorrupt-1": 16})
        scanner.scan()
        scanner.scan()
        assert DeferredQuestion.objects.count() == 1

    def test_same_artifact_set_not_reasked_after_answer(self) -> None:
        scanner = self._scanner({"db.sqlite3.precorrupt-1": 16})
        scanner.scan()
        DeferredQuestion.consume(DeferredQuestion.objects.get().pk, answer="keep them")

        scanner.scan()

        assert DeferredQuestion.objects.count() == 1

    def test_new_artifact_set_asked_once(self) -> None:
        self._scanner({"db.sqlite3.precorrupt-1": 16}).scan()
        DeferredQuestion.consume(DeferredQuestion.objects.get().pk, answer="keep them")
        grown = self._scanner({"db.sqlite3.precorrupt-1": 16, "db.sqlite3.precorrupt-2": 16})

        grown.scan()
        grown.scan()

        assert DeferredQuestion.objects.count() == 2
        assert "2 files" in shown_text(DeferredQuestion.pending().get())

    def test_a_box_with_nothing_to_reclaim_asks_nothing(self) -> None:
        assert StaleControlDbQuestionScanner(artifacts=list).scan() == []
        assert DeferredQuestion.objects.count() == 0

    def test_a_failing_probe_files_nothing_and_reaches_the_tick_error_surface(self) -> None:
        def boom() -> list[Path]:
            msg = "the data dir cannot be walked"
            raise OSError(msg)

        _, signals, error = _run_job(_ScannerJob(scanner=StaleControlDbQuestionScanner(artifacts=boom), overlay=""))

        assert (signals, error) == ([], "OSError: the data dir cannot be walked")
        assert DeferredQuestion.objects.count() == 0

    def test_a_failed_question_write_reaches_the_tick_error_surface(self) -> None:
        scanner = self._scanner({"db.sqlite3.precorrupt-1": 16})
        with patch.object(DeferredQuestion, "record", side_effect=RuntimeError("locked")):
            _, signals, error = _run_job(_ScannerJob(scanner=scanner, overlay=""))
        assert (signals, error) == ([], "RuntimeError: locked")
