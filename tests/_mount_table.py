"""Synthetic mount tables read through the real mount parser."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


@contextmanager
def patched_mount_table(*mounts: tuple[str, str, str]) -> Generator[None]:
    with TemporaryDirectory() as directory:
        table = Path(directory) / "mountinfo"
        lines = ["1 0 0:35 / / rw - overlay overlay rw"]
        lines.extend(
            f"{index} 1 {device} {root} {point} rw - ext4 /dev/x rw"
            for index, (device, root, point) in enumerate(mounts, start=2)
        )
        table.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with patch("teatree.mount_identity._MOUNTINFO", table):
            yield


@contextmanager
def missing_mount_table() -> Generator[None]:
    with TemporaryDirectory() as directory, patch("teatree.mount_identity._MOUNTINFO", Path(directory) / "missing"):
        yield
