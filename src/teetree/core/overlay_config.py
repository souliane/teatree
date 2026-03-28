"""Standalone overlay configuration reader.

Reads overlay.toml at the overlay root — no Django or Python imports required.
This allows hooks (bash scripts) to read overlay config without spinning up
the full Django stack.

Example overlay.toml::

    [overlay]
    name = "my-project"
    remote_patterns = ["git@github.com:org/my-project"]

    [skills]
    companion = ["ac-python", "ac-django"]
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class OverlayConfig:
    name: str = ""
    remote_patterns: list[str] = field(default_factory=list)
    companion_skills: list[str] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)


def load_overlay_config(overlay_root: Path) -> OverlayConfig:
    config_path = overlay_root / "overlay.toml"
    if not config_path.is_file():
        return OverlayConfig()

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    overlay_section = raw.get("overlay", {})
    skills_section = raw.get("skills", {})

    return OverlayConfig(
        name=str(overlay_section.get("name", "")),
        remote_patterns=list(overlay_section.get("remote_patterns", [])),
        companion_skills=list(skills_section.get("companion", [])),
        raw=raw,
    )
