"""A PATH that holds every executable the real one does except ``flock``."""

import os
from pathlib import Path


def path_without_flock(tmp_path: Path) -> str:
    shelf = tmp_path / "no-flock-bin"
    shelf.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        folder = Path(directory)
        if not folder.is_dir():
            continue
        for tool in folder.iterdir():
            if tool.name != "flock" and not (shelf / tool.name).exists() and os.access(tool, os.X_OK):
                (shelf / tool.name).symlink_to(tool)
    return str(shelf)
