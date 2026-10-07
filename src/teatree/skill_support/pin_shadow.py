"""A declared ``apm.yml`` skill pin is satisfied by an install, never by a same-named local folder.

An install root (``~/.agents/skills``, ``~/.claude/skills``, ``~/.codex/skills``) is
where ``t3 setup`` and apm put a pinned skill. A repo's or overlay's own ``skills/``
folder resolves first, so a directory there named like a pin silently replaces the
declared source everywhere the name is loaded (#4766). The resolver refuses that
instead. Realpath is deliberately not consulted: an install-root symlink into a live
clone of the declared repo is legitimate, and ``t3 doctor check`` audits provenance.
"""

from functools import cache
from pathlib import Path

from teatree.provisioning.declared import project_root_for_running_code, skills_declared_in_apm_manifest

_MANIFEST = "apm.yml"

_parsed: dict[Path, tuple[int, dict[str, str]]] = {}


class SkillShadowsDeclaredPinError(ValueError):
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

    No manifest declares nothing; an unparsable one raises
    :class:`~teatree.provisioning.declared.DeclarationUnreadableError`.
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
    specs = {dependency.name: dependency.source for dependency in skills_declared_in_apm_manifest(path)}
    _parsed[path] = (mtime, specs)
    return specs


def refuse_shadowed_pin(name: str, winner: Path) -> None:
    """Raise when *winner*, found outside every install root, satisfies a pinned *name*."""
    spec = declared_pin_specs().get(name)
    if spec is not None:
        raise SkillShadowsDeclaredPinError(name, spec, winner)


__all__ = ["SkillShadowsDeclaredPinError", "declared_pin_specs", "refuse_shadowed_pin"]
