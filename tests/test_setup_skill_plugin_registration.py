"""setup/SKILL.md must describe the current plugin-registration model.

Registration moved from a ``~/.claude/plugins/t3`` symlink to a record in
``installed_plugins.json`` (``installPath`` → main clone); ``t3 setup``
actively removes the legacy symlink (``_cleanup_legacy_plugin``). A skill
that still tells users to verify that symlink documents a path that no longer
exists.
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
