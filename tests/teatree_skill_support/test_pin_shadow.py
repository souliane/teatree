import os
from pathlib import Path

import pytest

from teatree.provisioning.declared import project_root_for_running_code
from teatree.skill_support.index import build_skill_index, resolve_skill_md
from teatree.skill_support.pin_shadow import (
    SkillPinRefusalError,
    SkillPinsUnreadableError,
    SkillShadowsDeclaredPinError,
    declared_pin_specs,
)
from tests._unreadable_apm_manifest import unreadable_running_manifest


def _skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return path


def _declared_spec(name: str) -> str:
    root = project_root_for_running_code()
    assert root is not None
    return declared_pin_specs(root / "apm.yml")[name]


def test_a_declared_pin_name_in_a_local_skills_folder_refuses_naming_both_sides(tmp_path: Path) -> None:
    local = tmp_path / "skills"
    shadow = _skill(local, "ac-django")
    _skill(Path.home() / ".agents" / "skills", "ac-django")

    with pytest.raises(SkillShadowsDeclaredPinError) as excinfo:
        resolve_skill_md("ac-django", [local, Path.home() / ".agents" / "skills"])

    assert isinstance(excinfo.value, SkillPinRefusalError)
    assert _declared_spec("ac-django") in str(excinfo.value)
    assert str(shadow) in str(excinfo.value)


def test_the_index_refuses_the_same_shadow_the_resolver_does(tmp_path: Path) -> None:
    _skill(tmp_path / "skills", "ac-django")

    with pytest.raises(SkillShadowsDeclaredPinError):
        build_skill_index([tmp_path / "skills"])


def test_a_declared_pin_found_only_in_an_install_root_resolves(tmp_path: Path) -> None:
    installed = _skill(Path.home() / ".agents" / "skills", "ac-django")

    assert resolve_skill_md("ac-django", [tmp_path / "skills", Path.home() / ".agents" / "skills"]) == installed


def test_a_claude_install_symlink_into_another_checkout_is_not_a_shadow(tmp_path: Path) -> None:
    clone = _skill(tmp_path / "live-clone", "ac-django")
    claude = Path.home() / ".claude" / "skills"
    claude.mkdir(parents=True)
    (claude / "ac-django").symlink_to(clone.parent)

    assert resolve_skill_md("ac-django", [claude]) == claude / "ac-django" / "SKILL.md"


def test_an_undeclared_name_in_a_local_folder_is_not_a_shadow(tmp_path: Path) -> None:
    local = _skill(tmp_path / "skills", "beta")

    assert resolve_skill_md("beta", [tmp_path / "skills"]) == local


def test_a_missing_manifest_declares_nothing(tmp_path: Path) -> None:
    assert declared_pin_specs(tmp_path / "apm.yml") == {}


@pytest.mark.parametrize(
    "body",
    ["dependencies: [unclosed", "- just\n- a list\n", "dependencies:\n  pip: []\n"],
    ids=["unparsable", "not-a-mapping", "no-apm-list"],
)
def test_an_unreadable_manifest_is_the_pin_refusal_naming_the_file_and_its_restore(tmp_path: Path, body: str) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(body, encoding="utf-8")

    with pytest.raises(SkillPinsUnreadableError) as excinfo:
        declared_pin_specs(manifest)

    assert isinstance(excinfo.value, SkillPinRefusalError)
    assert str(manifest) in str(excinfo.value)
    assert f"git -C {tmp_path} checkout HEAD -- apm.yml" in str(excinfo.value)


def test_the_resolver_refuses_on_an_unreadable_running_manifest(tmp_path: Path) -> None:
    _skill(tmp_path / "skills", "rules")

    with unreadable_running_manifest(tmp_path), pytest.raises(SkillPinsUnreadableError):
        resolve_skill_md("rules", [tmp_path / "skills"])


def test_the_declared_set_names_single_skill_specs_only(tmp_path: Path) -> None:
    (tmp_path / "apm.yml").write_text(
        "dependencies:\n  apm:\n  - owner/repo/skills/alpha#abc\n  - owner/bundle\n", encoding="utf-8"
    )

    assert declared_pin_specs(tmp_path / "apm.yml") == {"alpha": "owner/repo/skills/alpha#abc"}


def test_an_edited_manifest_is_reparsed_instead_of_served_from_the_memo(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text("dependencies:\n  apm:\n  - owner/repo/skills/alpha#abc\n", encoding="utf-8")
    assert declared_pin_specs(manifest) == {"alpha": "owner/repo/skills/alpha#abc"}

    manifest.write_text("dependencies:\n  apm:\n  - owner/repo/skills/beta#def\n", encoding="utf-8")
    stat = manifest.stat()
    os.utime(manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert declared_pin_specs(manifest) == {"beta": "owner/repo/skills/beta#def"}
