"""setup/SKILL.md must describe the current plugin-registration model.

The live plugin registration is an ``installed_plugins.json`` record whose
``installPath`` names the main clone. The setup skill must describe it.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SETUP_SKILL = _REPO_ROOT / "skills" / "setup" / "SKILL.md"


def test_setup_skill_does_not_tell_user_to_verify_legacy_symlink() -> None:
    text = _SETUP_SKILL.read_text(encoding="utf-8")
    assert "ls -la ~/.claude/plugins/t3" not in text


def test_setup_skill_names_the_registration_record() -> None:
    text = _SETUP_SKILL.read_text(encoding="utf-8")
    assert "installed_plugins.json" in text
