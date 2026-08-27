"""The overlays' declared skill sources, read once for both the gate and the install.

The declaration lives on the overlay (``OverlayConfig.skill_source_clones``), which
is domain state; acting on it — measuring drift, installing what it publishes — is
platform work. This module is the one place the two meet, so an overlay that
declares a source gets it BOTH measured and provisioned, with no second list to keep
in step. Splitting them is what let an overlay gate on skills its own provisioning
never installed.
"""

from pathlib import Path

from teatree.core.overlay_loader import get_all_overlays
from teatree.provisioning.skill_clone_install import CloneInstall, install_published_skills
from teatree.provisioning.skill_drift import SkillSourceClone


def declared_skill_sources() -> list[SkillSourceClone]:
    """Every registered overlay's declared skill-source clones, deduped."""
    sources: dict[tuple[str, tuple[str, ...], str], SkillSourceClone] = {}
    for overlay in get_all_overlays().values():
        config = getattr(overlay, "config", None)
        for clone in getattr(config, "skill_source_clones", []) or []:
            sources[clone.label, tuple(clone.paths), clone.ref] = clone
    return list(sources.values())


def install_declared_sources(*, link_dir: Path, cache_root: Path) -> list[CloneInstall]:
    """Install everything the registered overlays' declared skill sources publish."""
    return [
        install_published_skills(clone, link_dir=link_dir, cache_root=cache_root) for clone in declared_skill_sources()
    ]
