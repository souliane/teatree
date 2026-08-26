"""Guards the dockerized e2e lane's browser image against the locked ``playwright`` (souliane/teatree#3972).

``dev/Dockerfile.e2e`` builds on Microsoft's ``playwright/python`` image, which bakes
BROWSER BINARIES for one specific Playwright version. The Python client installed from
``uv.lock`` refuses to drive a browser build it does not recognise::

    BrowserType.launch: Executable doesn't exist at /ms-playwright/chromium_headless_shell-...
    Looks like Playwright was just updated to 1.62.0.
    Please update docker image as well.

That error lands at browser launch, BEFORE any test body runs, so a mismatch fails the
whole lane rather than one spec.

Nothing couples the two sides: ``uv lock`` moves the Python package whenever a resolution
changes, and the image tag is a string in a Dockerfile that no resolver ever visits. The
drift is therefore silent, and it has already compounded once -- #3972 was filed at a
1.58.0-image/1.59.0-package gap and sat open long enough for the lock to reach 1.62.0,
widening the gap by three minor versions while the tag never moved.

So this test asserts the two AGREE rather than pinning either to a hardcoded number: a
bump to the tag alone would re-rot on the next ``uv lock``. When it reds, bump the tag in
``dev/Dockerfile.e2e`` to the version named in the failure -- do not edit an expectation
here. If the locked version has no published image tag, that is the signal to hold the
``playwright`` bump back in the lock, not to silence this test.

Sibling pins guarding a Dockerfile string against a manifest: ``tests/test_claude_cli_pin.py``
and ``tests/test_claude_agent_sdk_pin.py``.
"""

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LOCK = _REPO_ROOT / "uv.lock"
_E2E_DOCKERFILE = _REPO_ROOT / "dev" / "Dockerfile.e2e"

#: The distribution whose browser build the image must carry. Matched exactly so the
#: ``pytest-playwright`` / ``pytest-playwright-visual`` packages never satisfy it.
_PACKAGE = "playwright"

#: ``FROM mcr.microsoft.com/playwright/python:v<version>-<distro>``. The ``v`` prefix and
#: the distro suffix are part of Microsoft's tag scheme, not of the version.
_FROM_RE = re.compile(
    r"^FROM\s+mcr\.microsoft\.com/playwright/python:v(?P<version>\d+\.\d+\.\d+)-(?P<distro>\w+)",
    re.MULTILINE,
)


def _locked_version(package: str) -> str:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    matches = [p["version"] for p in lock["package"] if p["name"] == package]
    assert len(matches) == 1, f"expected exactly one locked {package}, got {matches}"
    return matches[0]


def _image_tag() -> re.Match[str]:
    match = _FROM_RE.search(_E2E_DOCKERFILE.read_text(encoding="utf-8"))
    assert match is not None, (
        f"{_E2E_DOCKERFILE.relative_to(_REPO_ROOT)} no longer opens with a recognisable "
        "`FROM mcr.microsoft.com/playwright/python:v<x.y.z>-<distro>` line. If the lane "
        "deliberately moved off Microsoft's image, delete this test with that rationale; "
        "otherwise the browser/client parity it guards is now unchecked."
    )
    return match


def test_e2e_image_tag_matches_locked_playwright():
    """The e2e image's baked browsers match the ``playwright`` client ``uv.lock`` resolves."""
    locked = _locked_version(_PACKAGE)
    tag = _image_tag()

    assert tag["version"] == locked, (
        f"dev/Dockerfile.e2e builds on playwright/python:v{tag['version']}-{tag['distro']}, "
        f"but uv.lock resolves {_PACKAGE}=={locked}. The image's browser binaries and the "
        "Python client must be the same version or every dockerized e2e run dies at browser "
        f"launch. Bump the FROM tag to v{locked}-{tag['distro']} (confirm that tag is "
        "published at https://mcr.microsoft.com/v2/playwright/python/tags/list first)."
    )
