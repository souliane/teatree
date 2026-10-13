from contextlib import suppress
from pathlib import Path

import pytest

from teatree.utils import secrets
from teatree.utils.secrets import read_pass as bound_read_pass


def test_default_test_store_never_calls_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "pass-calls"
    fake = tmp_path / "pass"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{marker}'\nprintf 'value\\n'\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")

    bound_read_pass("example/token")
    with suppress(secrets.SecretNotFoundError):
        secrets.read_pass_required("example/token")
    secrets.write_pass("example/token", "value")
    secrets.remove_pass("example/token")
    secrets.pass_entry_exists("example/token")

    assert not marker.exists()
