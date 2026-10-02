# Runs gitleaks from the pinned prebuilt release, installing it on first use.
#
# It replaces the upstream `gitleaks/gitleaks` prek hook, whose `language: golang`
# built gitleaks from source on every cold cache. That meant a Go toolchain plus a
# module-graph resolution against proxy.golang.org and sum.golang.org on the runner,
# and it is what reddened blocking lint jobs three times on 2026-07-30, each time with
# `HTTP/2 stream ... INTERNAL_ERROR` on a different module. One sha256-pinned HTTPS
# GET of the official release archive removes both the toolchain and the
# checksum-database round trip; the pin is a stronger integrity statement than the Go
# proxy chain it replaces, because the digest is recorded here rather than fetched at
# install time.
#
# Bump procedure: change GITLEAKS_VERSION, replace every ARCHIVE_SHA256 digest from
# https://github.com/gitleaks/gitleaks/releases/download/v<version>/gitleaks_<version>_checksums.txt
# then regenerate BINARY_SHA256 — for each slug, sha256 the `gitleaks` member of the
# verified archive (upstream publishes archive digests only, not member ones).

import hashlib
import os
import platform
import stat
import sys
import tarfile
import tempfile
import urllib.request
from functools import cached_property
from pathlib import Path

GITLEAKS_VERSION = "8.30.1"

ARCHIVE_SHA256 = {
    "darwin_arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    "darwin_x64": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    "linux_arm64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    "linux_x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
}

# The digest of the `gitleaks` member INSIDE each archive. The archive digest alone
# cannot vouch for a cached binary: whoever can write the cache writes every file in it,
# so a digest recorded beside the binary certifies the impostor that wrote it. Recording
# the expected bytes HERE is what makes a pre-filled cache — under any root, XDG
# redirected or not — re-download rather than become the scanner.
BINARY_SHA256 = {
    "darwin_arm64": "ba52fb1bfabbcde42f032afad3d6e0b19dff8ed105229a16e7caa338bbc0e84f",
    "darwin_x64": "cee01fea7173f1b779dff188e1c26ecbcb4027d394acc573b23aaf0be260e291",
    "linux_arm64": "00e91bbe655bd7c47753e8cfe61cb76ea1a5d7e7702fe161ee40102b46b3823b",
    "linux_x64": "88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509",
}

DOWNLOAD_TIMEOUT = 120


class PinnedGitleaks:
    release_url = "https://github.com/gitleaks/gitleaks/releases/download/v{version}/{archive}"

    def __init__(self, version: str, cache_root: Path) -> None:
        self.version = version
        self.cache_root = cache_root

    @cached_property
    def platform_slug(self) -> str:
        system = platform.system().lower()
        if system not in {"darwin", "linux"}:
            message = f"no pinned gitleaks release for platform {system!r}"
            raise SystemExit(message)
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        return f"{system}_{arch}"

    @cached_property
    def archive_name(self) -> str:
        return f"gitleaks_{self.version}_{self.platform_slug}.tar.gz"

    @cached_property
    def path(self) -> Path:
        return self.cache_root / f"{self.version}-{self.platform_slug}" / "gitleaks"

    def install(self) -> Path:
        if not self._cached_binary_is_intact():
            self._extract(self._download_verified_archive())
        return self.path

    def _cached_binary_is_intact(self) -> bool:
        """Whether the cached binary is byte-for-byte the release pinned in this file.

        Existence is not the question. ``main`` ``execv``s whatever comes back and
        gitleaks' exit code IS the gate's verdict, so a truncated, half-written or
        swapped file at that path silently becomes the scanner. Re-derive the digest
        every run and re-install on any mismatch; the download is once per version.
        """
        if not self.path.exists():
            return False
        return hashlib.sha256(self.path.read_bytes()).hexdigest() == BINARY_SHA256[self.platform_slug]

    def _download_verified_archive(self) -> bytes:
        url = self.release_url.format(version=self.version, archive=self.archive_name)
        expected = ARCHIVE_SHA256[self.platform_slug]

        # A constant https:// release URL, and the payload is only ever used after its
        # sha256 matches the digest pinned above — see the inline justification.
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310 — sha256-verified
            payload = response.read()

        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected:
            message = f"{self.archive_name}: sha256 {digest} does not match pinned {expected}"
            raise SystemExit(message)
        return payload

    def _extract(self, payload: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as staging:
            archive_path = Path(staging) / self.archive_name
            archive_path.write_bytes(payload)

            with tarfile.open(archive_path) as archive:
                # `extractfile` raises KeyError on an absent name and returns None for a
                # non-regular member, so both have to be asked separately to fail with
                # something an operator can read.
                member = archive.extractfile("gitleaks") if "gitleaks" in archive.getnames() else None
                if member is None:
                    message = f"{self.archive_name}: no 'gitleaks' file in the archive"
                    raise SystemExit(message)
                unpacked = Path(staging) / "gitleaks"
                unpacked.write_bytes(member.read())

            expected = BINARY_SHA256[self.platform_slug]
            digest = hashlib.sha256(unpacked.read_bytes()).hexdigest()
            if digest != expected:
                message = f"{self.archive_name}: gitleaks sha256 {digest} does not match pinned {expected}"
                raise SystemExit(message)

            unpacked.chmod(unpacked.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            # An atomic rename, so two hooks racing on a cold cache can never observe a
            # half-written binary; the loser's copy dies with its staging directory.
            unpacked.replace(self.path)


def build_pinned_gitleaks() -> PinnedGitleaks:
    # No gate-specific cache override, and no need for one: BINARY_SHA256 decides what
    # `install()` may return, so pre-filling any root — XDG redirected or not — buys an
    # attacker a re-download, not an `exit 0` scanner. XDG is the ordinary cache
    # convention every other tool here already shares.
    xdg = os.environ.get("XDG_CACHE_HOME")
    cache_home = Path(xdg) if xdg else Path.home() / ".cache"
    return PinnedGitleaks(GITLEAKS_VERSION, cache_home / "gitleaks")


def main(argv: list[str]) -> None:
    binary = build_pinned_gitleaks().install()
    # execv, not subprocess: gitleaks' own exit code IS the gate's verdict, and handing
    # the process over means there is no wrapper left to swallow or rewrite it.
    os.execv(binary, [str(binary), *argv])  # noqa: S606 -- pinned, sha256-verified binary


if __name__ == "__main__":
    main(sys.argv[1:])
