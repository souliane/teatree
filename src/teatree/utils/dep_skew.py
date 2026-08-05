"""Declared-versus-INSTALLED version skew for the runtime dependencies (#4049).

:mod:`teatree.utils.dep_drift` answers "is a declared dependency missing?". That is
the bootstrap question, and it is deliberately zero-non-stdlib so the very command
that repairs a broken env can still run. It cannot answer the question that took a
host env three weeks past ``pyproject.toml`` without a word: a dependency that is
INSTALLED but too OLD. ``mcp>=2,<3`` against an installed ``mcp 1.28.1`` is not
missing, so the drift check passed while ``teatree.mcp.server``'s
``from mcp.server.mcpserver import MCPServer`` had no chance of resolving — the skew
only ever surfaced as an ``ImportError`` at the exact moment the server had to start.

Separate module because the answer needs real specifier semantics
(:mod:`packaging`), which is a non-stdlib import that has no business inside the
bootstrap-safe drift check. This one runs post-bootstrap, from ``t3 doctor``.
"""

import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from teatree.utils.dep_drift import normalize


@dataclass(frozen=True, slots=True)
class VersionSkew:
    """One declared requirement the installed environment does not satisfy."""

    name: str
    declared: str
    installed: str | None

    @property
    def summary(self) -> str:
        installed = self.installed or "NOT INSTALLED"
        return f"{self.name} declares {self.declared!r} but {installed} is installed"


def _requirements(pyproject_path: Path) -> list[Requirement]:
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    specs = data.get("project", {}).get("dependencies", []) or []
    parsed: list[Requirement] = []
    for spec in specs:
        try:
            parsed.append(Requirement(spec))
        except InvalidRequirement:
            continue
    return parsed


def _installed_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def find_version_skew(pyproject_path: Path) -> list[VersionSkew]:
    """Declared requirements this interpreter's installed dists do not satisfy.

    Environment markers are honoured only in the sense that an unparsable
    requirement is skipped — a requirement whose marker excludes this platform
    resolves to "not installed", which is the same signal the caller needs anyway
    (a dep the code imports but the env does not carry). Extras are ignored: the
    version of the base dist is what a stale ``uv tool`` env gets wrong.
    """
    skew: list[VersionSkew] = []
    for requirement in _requirements(pyproject_path):
        installed = _installed_version(requirement.name)
        if installed is not None and requirement.specifier.contains(installed, prereleases=True):
            continue
        if installed is None and requirement.marker is not None:
            continue
        skew.append(
            VersionSkew(
                name=normalize(requirement.name),
                declared=str(requirement.specifier) or "any",
                installed=installed,
            ),
        )
    return skew
