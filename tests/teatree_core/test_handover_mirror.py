"""Session hand-off mirroring — newest-pointer resolution and the symlink→copy fallback.

The ``latest`` pointer prefers a relative symlink but must still repoint when the
filesystem refuses symlinks (a bind-mount, a restrictive FS), so a hand-off is never
lost to a pointer-update failure.
"""

import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core import handover
from teatree.core.handover import newest_mirror, write_mirror
from teatree.core.models import SessionHandover


class TestNewestMirror:
    def _mirror(self, directory: Path, stamp: str) -> Path:
        path = directory / f"handover-sessA-{stamp}.md"
        path.write_text("payload\n", encoding="utf-8")
        return path

    def test_an_empty_directory_has_no_newest_mirror(self, tmp_path: Path) -> None:
        assert newest_mirror(tmp_path) is None

    def test_the_lexicographically_latest_stamp_wins(self, tmp_path: Path) -> None:
        self._mirror(tmp_path, "20260101T000000_000000")
        newest = self._mirror(tmp_path, "20260722T120000_000000")
        self._mirror(tmp_path, "20260315T090000_000000")
        assert newest_mirror(tmp_path) == newest

    def test_a_non_mirror_file_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "unrelated.md").write_text("x\n", encoding="utf-8")
        assert newest_mirror(tmp_path) is None


class TestWriteMirrorPointerFallback(TestCase):
    def _handover(self) -> SessionHandover:
        return SessionHandover.objects.create_handover(
            from_session="sess-from", to_session="sess-to", payload="the durable state"
        )

    def test_a_symlink_refusing_filesystem_falls_back_to_a_copy(self) -> None:
        pointer = Path(self.enterContext(tempfile.TemporaryDirectory())) / "latest.md"

        def _refuse_symlink(_self: Path, *_a: object, **_k: object) -> None:
            msg = "this filesystem refuses symlinks"
            raise OSError(msg)

        with mock.patch.object(handover.Path, "symlink_to", _refuse_symlink):
            unique = write_mirror(self._handover(), pointer)

        # The pointer could not be symlinked, so it was populated by copying the newest
        # mirror's content instead — the hand-off is readable either way.
        assert pointer.is_file()
        assert not pointer.is_symlink()
        assert pointer.read_text(encoding="utf-8") == unique.read_text(encoding="utf-8")
        assert "the durable state" in pointer.read_text(encoding="utf-8")
