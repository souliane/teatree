"""The runtime image carries the codex CLI the reviewer's second pass shells out to (#159).

A tool a role SHELLS OUT to has to be IN the image (deploy/Dockerfile's own rule).
The reviewer brief runs ``codex-elite-review``, which needs ``codex`` on PATH; the
pin is exact so a build cannot drift to whatever ``latest`` resolves that day.
"""

import re
from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"
_CODEX_PIN = re.compile(r"@openai/codex@(\d+\.\d+\.\d+)\b")
_EXPECTED_CODEX_VERSION = "0.155.1"


def test_runtime_image_pins_codex_cli_exactly_once() -> None:
    assert _CODEX_PIN.findall(_DOCKERFILE.read_text(encoding="utf-8")) == [_EXPECTED_CODEX_VERSION]


def test_codex_pin_sits_on_the_global_npm_install_line() -> None:
    lines = [line for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines() if "@openai/codex@" in line]
    assert lines
    assert all("npm install -g" in line for line in lines)
