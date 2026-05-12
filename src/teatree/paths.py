"""XDG-compliant data paths — leaf module with no teatree dependencies."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "teatree"


def get_data_dir(namespace: str) -> Path:
    data_dir = DATA_DIR / namespace
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
