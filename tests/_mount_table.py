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
        prior = [(1, "/")]
        for index, (device, root, point) in enumerate(mounts, start=2):
            parent = max(
                (earlier for earlier in prior if point == earlier[1] or point.startswith(f"{earlier[1].rstrip('/')}/")),
                key=lambda earlier: (len(earlier[1]), earlier[0]),
            )[0]
            lines.append(f"{index} {parent} {device} {root} {point} rw - ext4 /dev/x rw")
            prior.append((index, point))
        table.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with patch("teatree.mount_identity._MOUNTINFO", table):
            yield


@contextmanager
def missing_mount_table() -> Generator[None]:
    with TemporaryDirectory() as directory, patch("teatree.mount_identity._MOUNTINFO", Path(directory) / "missing"):
        yield
