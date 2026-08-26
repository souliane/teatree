# test-path: cross-cutting — tests scripts/hooks/gitleaks.py, which has no src/teatree/ mirror.
"""The pinned-gitleaks installer only ever hands back a binary it verified.

`main` `execv`s whatever `install()` returns, and gitleaks' exit code IS the verdict of
the repo's only secret gate — so anything that can decide the returned bytes can turn
that gate into `exit 0` and nothing downstream would notice.
"""

import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.hooks.gitleaks import GITLEAKS_VERSION, PinnedGitleaks, build_pinned_gitleaks

_REAL_BODY = b"#!/bin/sh\nexit 1\n"
_IMPOSTOR_BODY = b"#!/bin/sh\nexit 0\n"


def _archive(body: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("gitleaks")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


@pytest.fixture
def installed(tmp_path: Path) -> PinnedGitleaks:
    pinned = PinnedGitleaks(GITLEAKS_VERSION, tmp_path / "cache")
    with patch.object(PinnedGitleaks, "_download_verified_archive", return_value=_archive(_REAL_BODY)):
        pinned.install()
    return pinned


class TestACachedBinaryIsVerifiedBeforeItIsTrusted:
    def test_the_first_install_writes_the_downloaded_binary(self, installed: PinnedGitleaks) -> None:
        assert installed.path.read_bytes() == _REAL_BODY

    def test_a_warm_cache_does_not_re_download(self, installed: PinnedGitleaks) -> None:
        with patch.object(PinnedGitleaks, "_download_verified_archive") as download:
            installed.install()
        download.assert_not_called()

    def test_a_swapped_cached_binary_is_replaced_rather_than_returned(self, installed: PinnedGitleaks) -> None:
        installed.path.write_bytes(_IMPOSTOR_BODY)

        with patch.object(PinnedGitleaks, "_download_verified_archive", return_value=_archive(_REAL_BODY)):
            returned = installed.install()

        assert returned.read_bytes() == _REAL_BODY

    def test_a_cached_binary_with_no_recorded_digest_is_replaced(self, installed: PinnedGitleaks) -> None:
        for sidecar in installed.path.parent.iterdir():
            if sidecar != installed.path:
                sidecar.unlink()
        installed.path.write_bytes(_IMPOSTOR_BODY)

        with patch.object(PinnedGitleaks, "_download_verified_archive", return_value=_archive(_REAL_BODY)):
            returned = installed.install()

        assert returned.read_bytes() == _REAL_BODY

    def test_the_recorded_digest_is_of_the_binary_actually_written(self, installed: PinnedGitleaks) -> None:
        recorded = {
            path.read_text(encoding="utf-8").strip()
            for path in installed.path.parent.iterdir()
            if path != installed.path
        }

        assert recorded == {hashlib.sha256(_REAL_BODY).hexdigest()}


class TestTheCacheRootIsNotRedirectable:
    def test_no_environment_variable_moves_the_cache_off_the_xdg_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A redirectable root is a one-line disable of the gate: point it at a directory
        # holding an `exit 0` impostor and `install()` hands that back as the scanner.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("GITLEAKS_CACHE_DIR", str(tmp_path / "attacker"))

        assert build_pinned_gitleaks().cache_root == tmp_path / "xdg" / "gitleaks"
