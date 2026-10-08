"""Point the running code's ``apm.yml`` at a manifest that exists but cannot be parsed."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from teatree.skill_support import pin_shadow

UNPARSABLE_MANIFEST = "dependencies: [unclosed"


@contextmanager
def unreadable_running_manifest(directory: Path) -> Iterator[Path]:
    manifest = directory / "apm.yml"
    manifest.write_text(UNPARSABLE_MANIFEST, encoding="utf-8")
    with patch.object(pin_shadow, "_running_code_manifest", return_value=manifest):
        yield manifest
