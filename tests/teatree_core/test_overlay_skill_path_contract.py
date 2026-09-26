"""Every registered overlay's ``skill_path`` names one real skill, whichever helper reads it."""

from pathlib import Path

from teatree.agents.skill_injection import _bare_skill_name, _explicit_load_name, _resolve_skill_md, harness_skills_dirs
from teatree.core.overlay_loader import get_all_overlays
from teatree.core.overlay_skills import overlay_skills_root


def _declared_skill_paths() -> dict[str, tuple[str, list[Path]]]:
    declared: dict[str, tuple[str, list[Path]]] = {}
    for name, overlay in get_all_overlays().items():
        metadata = overlay.metadata.get_skill_metadata()
        skill_path = str(metadata.get("skill_path", "")).strip()
        if not skill_path:
            continue
        root = overlay_skills_root(metadata, None)
        declared[name] = (skill_path, [*([root] if root is not None else []), *harness_skills_dirs()])
    return declared


def test_the_bundled_overlay_is_among_the_checked_overlays() -> None:
    assert "t3-teatree" in _declared_skill_paths()


def test_both_name_helpers_agree_on_every_overlay_skill_path() -> None:
    disagreements = {
        name: (_bare_skill_name(path), _explicit_load_name(path))
        for name, (path, _dirs) in _declared_skill_paths().items()
        if _bare_skill_name(path) != _explicit_load_name(path)
    }

    assert disagreements == {}


def test_every_overlay_skill_path_resolves_to_a_skill_md() -> None:
    unresolved = [
        name for name, (path, dirs) in _declared_skill_paths().items() if _resolve_skill_md(path, dirs) is None
    ]

    assert unresolved == []
