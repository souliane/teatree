"""The e2e image ships the browsers of exactly the Playwright the compose lane installs.

The compose ``e2e`` service mounts this tree and runs ``uv run`` against its own
``uv.lock``, so the Python ``playwright`` it imports is the locked one. That package
looks for the browser build of its OWN release; an image built for another release
has none, and every browser test dies with "Executable doesn't exist".
"""
# test-path: cross-cutting — pins a Dockerfile to a lockfile; neither is a teatree module.

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_IMAGE_TAG = re.compile(r"^FROM mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-", re.MULTILINE)


def _image_playwright_version() -> str:
    match = _IMAGE_TAG.search((_ROOT / "dev" / "Dockerfile.e2e").read_text(encoding="utf-8"))
    assert match, "dev/Dockerfile.e2e no longer builds FROM a playwright/python:v<X.Y.Z>-* image"
    return match.group(1)


def _locked_playwright_version() -> str:
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [package["version"] for package in lock["package"] if package["name"] == "playwright"]
    assert len(versions) == 1, f"uv.lock resolves playwright {len(versions)} times: {versions}"
    return versions[0]


def test_the_e2e_image_matches_the_locked_playwright() -> None:
    assert _image_playwright_version() == _locked_playwright_version()
