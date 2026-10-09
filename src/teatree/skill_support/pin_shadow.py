"""A declared ``apm.yml`` skill pin is satisfied by an install, never by a same-named local folder.

An install root (``~/.agents/skills``, ``~/.claude/skills``, ``~/.codex/skills``) is
where ``t3 setup`` and apm put a pinned skill. A repo's or overlay's own ``skills/``
folder resolves first, so a directory there named like a pin silently replaces the
declared source everywhere the name is loaded (#4766). The resolver refuses that
instead. Realpath is deliberately not consulted here: ``t3 doctor check`` FAILs an install
that resolves outside the install roots or into a checkout.
"""

from functools import cache
from pathlib import Path

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    project_root_for_running_code,
    skills_declared_in_apm_manifest,
)

_MANIFEST = "apm.yml"

_parsed: dict[Path, tuple[int, dict[str, str]]] = {}


class SkillPinRefusalError(ValueError):
    """The declared skill pins refuse this resolution; every skill-resolving surface reports it, none dispatches."""


class SkillPinsUnreadableError(SkillPinRefusalError):
    """``apm.yml`` exists but its pins cannot be read, so no resolution can prove a pin is honoured."""

    def __init__(self, manifest: Path, reason: str) -> None:
        super().__init__(
            f"{reason}. No skill resolves until the declared pins are readable: fix its YAML, or restore the "
            f"committed copy with `git -C {manifest.parent} checkout HEAD -- {manifest.name}`."
        )


class SkillShadowsDeclaredPinError(SkillPinRefusalError):
    """A folder no installer manages satisfies a skill name ``apm.yml`` pins."""

    def __init__(self, name: str, spec: str, winner: Path) -> None:
        self.name = name
        self.spec = spec
        self.winner = winner
        super().__init__(
            f"skill {name!r} is declared as the apm pin `{spec}`, but {winner} resolves first from a folder "
            "no installer manages, so the declared skill never loads. Delete or rename the local copy; "
            "`t3 setup` installs the pinned one."
        )


@cache
def _running_code_manifest() -> Path | None:
    root = project_root_for_running_code()
    return None if root is None else root / _MANIFEST


def declared_pin_specs(manifest: Path | None = None) -> dict[str, str]:
    """``{skill name: spec}`` for every single-skill ``dependencies.apm`` entry.

    No manifest declares nothing; one that exists but cannot be read raises :class:`SkillPinsUnreadableError`.
    """
    path = _running_code_manifest() if manifest is None else manifest
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    cached = _parsed.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        specs = {dependency.name: dependency.source for dependency in skills_declared_in_apm_manifest(path)}
    except DeclarationUnreadableError as exc:
        raise SkillPinsUnreadableError(path, str(exc)) from exc
    _parsed[path] = (mtime, specs)
    return specs


def refuse_shadowed_pin(name: str, winner: Path) -> None:
    """Raise when *winner*, found outside every install root, satisfies a pinned *name*."""
    spec = declared_pin_specs().get(name)
    if spec is not None:
        raise SkillShadowsDeclaredPinError(name, spec, winner)


__all__ = [
    "SkillPinRefusalError",
    "SkillPinsUnreadableError",
    "SkillShadowsDeclaredPinError",
    "declared_pin_specs",
    "refuse_shadowed_pin",
]
