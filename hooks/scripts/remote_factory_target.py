"""Cold SessionStart answer for whether this session targets a remote factory."""

import os
import sys
from pathlib import Path

sys.modules.setdefault("remote_factory_target", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.remote_factory_target", sys.modules[__name__])


def remote_factory_target_active() -> bool:
    target = os.environ.get("T3_FACTORY_TARGET", "").strip()
    if target:
        return target != "local"

    data_dir = os.environ.get("T3_DATA_DIR", "")
    if data_dir:
        root = Path(data_dir)
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        root = Path(xdg) / "teatree" if xdg else Path.home() / ".local/share/teatree"

    try:
        with (root / "factory-target").open(encoding="utf-8") as source:
            return any(line.strip() and not line.lstrip().startswith("#") for line in source)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, UnicodeError):
        return True
