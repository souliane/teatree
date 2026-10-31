"""The factory is Linux only: a macOS arm survives only on the interactive side (#5857).

The worker, the loops, the deploy scripts and the image run on Linux. macOS is supported for the
interactive skill alone, so a term that names a macOS-only mechanism (or the Docker Desktop shims and the
host-pressure feed that existed to bridge a non-Linux host) may appear in a tracked file only where the
interactive side needs it. The scan walks every tracked file, so a new arm anywhere else turns it red.
"""

import re
from pathlib import Path

import pytest

from teatree.utils.run import run_checked

_ROOT = Path(__file__).resolve().parents[2]

_MACOS_ARMS = re.compile(
    r"darwin|sysctl|vm_stat|launchd|launchctl|osxfs|fakeowner|pinentry-mac|TEATREE_HOST_OS|afconvert"
    r"|docker desktop|host[-_]pressure",
    re.IGNORECASE,
)

#: Hook venvs are named per OS and architecture so a checkout shared between a Mac and a Linux box
#: never runs the wrong interpreter; the venv helpers and the tests that pin that naming say `darwin`.
#: `settings.json` is the permission set recommended to an attended session, which runs on a Mac too.
_INTERACTIVE_SIDE = frozenset(
    {
        "settings.json",
        "src/teatree/utils/venv_artifacts.py",
        "tests/teatree_cli/doctor/test_checks_interpreter_plane.py",
        "tests/teatree_cli/doctor/test_venv_interpreter_check.py",
        "tests/teatree_core/cleanup/test_artifact_eviction.py",
        "tests/teatree_utils/test_venv_artifacts.py",
        "tests/test_hook_uv_resolution_cache.py",
    }
)

_THIS_FILE = "tests/conformance/test_factory_is_linux_only.py"
_BINARY_SUFFIXES = (".png", ".jpg", ".ico", ".woff2", ".gz", ".sqlite3")


def _tracked_files() -> list[str]:
    listing = run_checked(["git", "-C", str(_ROOT), "ls-files", "-z"]).stdout
    return [name for name in listing.split("\0") if name and not name.endswith(_BINARY_SUFFIXES)]


def _arms_in(relative: str) -> list[str]:
    try:
        text = (_ROOT / relative).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return []
    return sorted({match.group(0).lower() for match in _MACOS_ARMS.finditer(text)})


def test_no_macos_arm_survives_outside_the_interactive_side() -> None:
    offenders = {
        name: arms
        for name in _tracked_files()
        if name != _THIS_FILE and name not in _INTERACTIVE_SIDE and (arms := _arms_in(name))
    }

    assert not offenders, f"macOS arms outside the interactive side: {offenders}"


def test_every_interactive_side_exemption_still_needs_its_exemption() -> None:
    stale = sorted(name for name in _INTERACTIVE_SIDE if not _arms_in(name))

    assert not stale, f"exempt files that no longer name a macOS arm — drop them from the roster: {stale}"


def test_the_scan_sees_the_files_that_used_to_carry_arms() -> None:
    tracked = set(_tracked_files())

    assert {"hooks/scripts/statusline.sh", "deploy/deploy.sh", "src/teatree/utils/ram_probe.py"} <= tracked
    assert len(tracked) > 1000


@pytest.mark.parametrize(
    "removed",
    [
        "hooks/scripts/host-pressure-publish.zsh",
        "deploy/install-host-pressure.zsh",
        "src/teatree/utils/host_pressure.py",
        "src/teatree/utils/host_pressure_sampler.py",
        "src/teatree/core/factory/host_pressure_health.py",
        "src/teatree/core/speak.py",
        "src/teatree/core/speak_cleaning.py",
        "src/teatree/core/presence.py",
        "src/teatree/config/speak.py",
        "src/teatree/backends/slack/audio_upload.py",
        "tests/teatree_utils/test_host_pressure_publisher.py",
        "tests/teatree_core/test_host_pressure.py",
    ],
)
def test_the_removed_files_stay_removed(removed: str) -> None:
    assert not (_ROOT / removed).exists()
    assert removed not in _tracked_files()


def test_the_msteams_backend_package_is_gone() -> None:
    assert not (_ROOT / "src/teatree/backends/msteams").exists()


def _compatibility(skill: Path) -> str:
    frontmatter = skill.read_text(encoding="utf-8").split("---", 2)[1]
    return next(line for line in frontmatter.splitlines() if line.startswith("compatibility:")).split(":", 1)[1].strip()


def test_every_skill_but_the_interactive_one_is_linux_only() -> None:
    skills = sorted((_ROOT / "skills").glob("*/SKILL.md"))
    wrong = {
        skill.parent.name: _compatibility(skill)
        for skill in skills
        if skill.parent.name != "interactive" and not _compatibility(skill).startswith("Linux")
    }

    assert len(skills) > 20
    assert not wrong, f"skills whose compatibility does not start with Linux: {wrong}"


def test_the_interactive_skill_is_macos_and_linux_and_states_the_portability_rule() -> None:
    skill = _ROOT / "skills" / "interactive" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")

    assert _compatibility(skill).startswith("macOS/Linux")
    for rule in ("bash 3.2", "Python 3.9", "GNU-only flags", "must also work on Linux"):
        assert rule in text, rule


@pytest.mark.parametrize("document", ["README.md", "docs/install.md", "BLUEPRINT.md", "deploy/README.md"])
def test_the_docs_state_the_factory_is_linux_only(document: str) -> None:
    text = (_ROOT / document).read_text(encoding="utf-8")

    assert "Linux only" in text, document
    assert "macOS is supported for the interactive skill alone" in text, document
