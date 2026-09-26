"""Every non-empty ``*_skill`` code default must resolve to a shipped skill.

A code default is what a fresh box starts with BEFORE any DB override lands
(#4853): ``UserSettings`` (:mod:`teatree.config.settings`) and ``OverlayConfig``
(:mod:`teatree.core.overlay`) each declare ``*_skill`` fields whose default
names the skill init's strict dispatched-skill check (#4851) then requires to
be loadable. A default that names a skill nobody shipped passes review — the
string looks plausible — and only fails at runtime, on every fresh deploy that
never overrides it, with the worker crash-looping on
``FATAL ... strict agent-skill setup is incomplete``.

Enumeration is generic over ``dataclasses.fields`` / ``model_fields`` rather
than a hand-maintained name list, so a *new* ``*_skill`` field is covered the
moment it is declared — nobody has to remember to extend this file.
"""

import dataclasses
from pathlib import Path

import pytest

from teatree.config.settings import UserSettings
from teatree.core.overlay import OverlayConfig

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def _dataclass_skill_defaults() -> list[tuple[str, str]]:
    return [
        (f"UserSettings.{f.name}", f.default)
        for f in dataclasses.fields(UserSettings)
        if f.name.endswith("_skill") and isinstance(f.default, str) and f.default.strip()
    ]


def _overlay_config_skill_defaults() -> list[tuple[str, str]]:
    return [
        (f"OverlayConfig.{name}", info.default)
        for name, info in OverlayConfig.model_fields.items()
        if name.endswith("_skill") and isinstance(info.default, str) and info.default.strip()
    ]


SKILL_DEFAULTS = _dataclass_skill_defaults() + _overlay_config_skill_defaults()


def test_the_catalogue_is_where_this_test_thinks_it_is() -> None:
    assert (SKILLS_DIR / "rules" / "SKILL.md").is_file(), (
        f"{SKILLS_DIR} is not the skills catalogue — every resolution below would be vacuously true"
    )


def test_at_least_one_skill_default_is_enumerated() -> None:
    # Anti-vacuity: if every ``*_skill`` field were renamed away, the parametrized
    # test below would silently collect zero cases and pass having checked nothing.
    assert SKILL_DEFAULTS


@pytest.mark.parametrize(("source", "name"), SKILL_DEFAULTS, ids=lambda value: value)
def test_every_skill_default_resolves_to_a_shipped_skill(source: str, name: str) -> None:
    assert (SKILLS_DIR / name / "SKILL.md").is_file(), (
        f"{source} defaults to {name!r}, which has no skills/{name}/SKILL.md. A fresh box that never "
        f"overrides this setting fails the strict dispatched-skill check at init and crash-loops."
    )
