# test-path: cross-cutting — tests scripts/hooks/gitleaks.py, which has no src/teatree/ mirror.
"""The pinned-gitleaks installer only ever hands back a binary it verified.

`main` `execv`s whatever `install()` returns, and gitleaks' exit code IS the verdict of
the repo's only secret gate — so anything that can decide the returned bytes can turn
that gate into `exit 0` and nothing downstream would notice.
"""

import hashlib
import io
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.hooks import gitleaks
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


@contextmanager
def _pinned_to(pinned: PinnedGitleaks, body: bytes) -> Iterator[None]:
    """Aim the pin this platform's slug resolves against at *body*."""
    with patch.dict(gitleaks.BINARY_SHA256, {pinned.platform_slug: hashlib.sha256(body).hexdigest()}):
        yield


@pytest.fixture
def installed(tmp_path: Path) -> Iterator[PinnedGitleaks]:
    pinned = PinnedGitleaks(GITLEAKS_VERSION, tmp_path / "cache")
    with _pinned_to(pinned, _REAL_BODY):
        with patch.object(PinnedGitleaks, "_download_verified_archive", return_value=_archive(_REAL_BODY)):
            pinned.install()
        yield pinned


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

    def test_an_archive_carrying_the_wrong_binary_is_refused(self, tmp_path: Path) -> None:
        pinned = PinnedGitleaks(GITLEAKS_VERSION, tmp_path / "cache")

        with (
            _pinned_to(pinned, _REAL_BODY),
            patch.object(PinnedGitleaks, "_download_verified_archive", return_value=_archive(_IMPOSTOR_BODY)),
            pytest.raises(SystemExit),
        ):
            pinned.install()

        assert not pinned.path.exists()


class TestRedirectingTheCacheRootCannotDisableTheGate:
    """The root follows XDG like every other tool's, and a redirect buys nothing.

    A root the caller can name is a root the caller can pre-fill, and the cache used to
    vouch for its own contents with a digest written beside the binary — so an `exit 0`
    impostor plus its matching sidecar WAS the whole gate, one variable away. The pin
    now lives in the source, so a planted cache is re-downloaded over instead.
    """

    def test_no_gate_specific_variable_moves_the_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("GITLEAKS_CACHE_DIR", str(tmp_path / "attacker"))

        assert build_pinned_gitleaks().cache_root == tmp_path / "xdg" / "gitleaks"

    def test_a_self_certifying_impostor_under_a_redirected_root_is_not_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        pinned = build_pinned_gitleaks()
        pinned.path.parent.mkdir(parents=True, exist_ok=True)
        pinned.path.write_bytes(_IMPOSTOR_BODY)
        # The sidecar the cache used to certify itself with — planting it is the bypass.
        pinned.path.with_suffix(".sha256").write_text(hashlib.sha256(_IMPOSTOR_BODY).hexdigest(), encoding="utf-8")

        with (
            _pinned_to(pinned, _REAL_BODY),
            patch.object(PinnedGitleaks, "_download_verified_archive", return_value=_archive(_REAL_BODY)),
        ):
            returned = pinned.install()

        assert returned.read_bytes() == _REAL_BODY
