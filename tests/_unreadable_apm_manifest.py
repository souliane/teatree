"""Point the running code's ``apm.yml`` at a manifest that exists but cannot be parsed."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from teatree.skill_support import pin_shadow

UNPARSABLE_MANIFEST = b"dependencies: [unclosed"
NOT_UTF8_MANIFEST = b"dependencies:\n  apm:\n  - caf\xe9/skills/x#abc\n"


@contextmanager
def unreadable_running_manifest(directory: Path, body: bytes = UNPARSABLE_MANIFEST) -> Iterator[Path]:
    manifest = directory / "apm.yml"
    manifest.write_bytes(body)
    with patch.object(pin_shadow, "_running_code_manifest", return_value=manifest):
        yield manifest
