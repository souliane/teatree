# test-path: cross-cutting — pins deploy/Dockerfile to the provisioning module's production version.
import re
from pathlib import Path

from teatree.provisioning.skills_cli import SKILLS_CLI_VERSION

_DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"
_SKILLS_PIN = re.compile(r"(?<![\w@/-])skills@(\d+\.\d+\.\d+)\b")


def test_runtime_image_pins_skills_cli_to_production_version() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert _SKILLS_PIN.findall(text) == [SKILLS_CLI_VERSION]


def test_skills_pin_sits_on_the_global_npm_install_line() -> None:
    lines = [line for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines() if "skills@" in line]
    assert lines
    assert all("npm install -g" in line for line in lines)
