"""Enumerate the dependencies teatree's own configuration declares REQUIRED.

Epic #3445's acceptance principle: a dependency the configuration mandates but
nothing provisioned must be a loud ``t3 doctor`` FAIL, never silence. That only
holds if the enumeration comes from the **declaration surfaces themselves** — a
second, hand-maintained list in the checker would reproduce the bug the moment
something new is mandated and nobody remembers to add it.

Three surfaces, one per dependency kind:

* ``apm.yml`` ``dependencies.apm`` — the mandated companion skills
* ``pyproject.toml`` ``[tool.teatree.provisioning] required_binaries`` — the tools
* ``~/.claude/settings.json`` ``enabledPlugins`` — the enabled integrations

An unreadable surface raises :class:`DeclarationUnreadableError` rather than
returning an empty list: "I could not read the mandate" and "nothing is
mandated" are different answers, and collapsing them is exactly the silence
this gate exists to remove.
"""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

type DependencyKind = Literal["skill", "binary", "integration"]

_APM_MANIFEST = "apm.yml"
_PYPROJECT = "pyproject.toml"
_CLAUDE_SETTINGS = (".claude", "settings.json")
_MIN_SKILL_SPEC_SEGMENTS = 3

_SKILL_REMEDIATION = (
    "run `t3 setup` in this environment (it provisions every declared skill dependency "
    "idempotently), or install it directly with `apm install -g --target claude`"
)
_BINARY_REMEDIATION = "install it and put it on PATH, then re-run `t3 doctor check`"
_INTEGRATION_REMEDIATION = "run `t3 setup` to re-register the plugin, or disable it in ~/.claude/settings.json"


def project_root_for_running_code() -> Path | None:
    """The checkout whose declaration surfaces govern the code that is running.

    Deliberately NOT :func:`teatree.find_project_root`, which redirects a
    worktree back to its primary clone: the mandate being verified is the one
    shipping with THIS code, so a worktree must be gated against its own
    manifest rather than the clone's.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / _APM_MANIFEST).is_file() or (candidate / _PYPROJECT).is_file():
            return candidate
    return None


class DeclarationUnreadableError(RuntimeError):
    """A declaration surface could not be read, so its mandates are unknown."""


@dataclass(frozen=True, slots=True)
class DeclaredDependency:
    """One dependency the configuration mandates, with where it is declared."""

    kind: DependencyKind
    name: str
    declared_in: str
    remediation: str
    #: The declaration's raw spec, when the kind carries a fetchable source
    #: (``<owner>/<repo>/<subpath>#<ref>`` for a skill). Empty otherwise.
    source: str = ""


def _read_text(path: Path, surface: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"{surface} is not readable at {path}: {exc}"
        raise DeclarationUnreadableError(msg) from exc


def skills_declared_in_apm_manifest(manifest: Path) -> list[DeclaredDependency]:
    """Mandated skills from ``apm.yml``'s ``dependencies.apm`` list.

    An entry of shape ``<owner>/<repo>/<subpath>[#<ref>]`` names ONE skill (its
    last path segment). A two-segment entry is a whole-repo bundle that names no
    single skill, so it declares nothing enumerable here.
    """
    try:
        data = yaml.safe_load(_read_text(manifest, _APM_MANIFEST))
    except yaml.YAMLError as exc:
        msg = f"{_APM_MANIFEST} is not parsable at {manifest}: {exc}"
        raise DeclarationUnreadableError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{_APM_MANIFEST} at {manifest} is not a mapping"
        raise DeclarationUnreadableError(msg)
    dependencies = data.get("dependencies")
    entries = dependencies.get("apm") if isinstance(dependencies, dict) else None
    if not isinstance(entries, list):
        msg = f"{_APM_MANIFEST} at {manifest} declares no dependencies.apm list"
        raise DeclarationUnreadableError(msg)

    declared: list[DeclaredDependency] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        segments = entry.split("#", 1)[0].strip("/").split("/")
        if len(segments) < _MIN_SKILL_SPEC_SEGMENTS:
            continue
        declared.append(
            DeclaredDependency(
                kind="skill",
                name=segments[-1],
                declared_in=f"{_APM_MANIFEST} → dependencies.apm",
                remediation=_SKILL_REMEDIATION,
                source=entry.strip(),
            )
        )
    return declared


def binaries_declared_in_pyproject(pyproject: Path) -> list[DeclaredDependency]:
    """Required tools from ``[tool.teatree.provisioning] required_binaries``."""
    try:
        data = tomllib.loads(_read_text(pyproject, _PYPROJECT))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{_PYPROJECT} is not parsable at {pyproject}: {exc}"
        raise DeclarationUnreadableError(msg) from exc
    table = data.get("tool", {}).get("teatree", {}).get("provisioning", {})
    binaries = table.get("required_binaries") if isinstance(table, dict) else None
    if not isinstance(binaries, list):
        msg = f"{_PYPROJECT} at {pyproject} declares no [tool.teatree.provisioning] required_binaries"
        raise DeclarationUnreadableError(msg)
    return [
        DeclaredDependency(
            kind="binary",
            name=name,
            declared_in=f"{_PYPROJECT} → [tool.teatree.provisioning].required_binaries",
            remediation=_BINARY_REMEDIATION,
        )
        for name in binaries
        if isinstance(name, str) and name
    ]


def integrations_declared_in_claude_settings(settings: Path) -> list[DeclaredDependency]:
    """Enabled agent plugins from ``~/.claude/settings.json``'s ``enabledPlugins``.

    Absent settings declare nothing — a box with no agent runtime configured has
    enabled no integration, which is a genuine empty rather than an unread mandate.
    """
    if not settings.is_file():
        return []
    try:
        data = json.loads(_read_text(settings, "~/.claude/settings.json"))
    except json.JSONDecodeError as exc:
        msg = f"~/.claude/settings.json is not parsable at {settings}: {exc}"
        raise DeclarationUnreadableError(msg) from exc
    enabled = data.get("enabledPlugins") if isinstance(data, dict) else None
    if not isinstance(enabled, dict):
        return []
    return [
        DeclaredDependency(
            kind="integration",
            name=plugin_id,
            declared_in="~/.claude/settings.json → enabledPlugins",
            remediation=_INTEGRATION_REMEDIATION,
        )
        for plugin_id, value in enabled.items()
        if value is True
    ]


@dataclass(frozen=True, slots=True)
class Enumeration:
    """What the declaration surfaces mandate, plus the ones that could not be read.

    The two halves are kept apart on purpose: one unreadable surface must not
    suppress the mandates of the others, and it must not pass as "nothing is
    mandated" either — it is reported as its own unverified-surface finding.
    """

    dependencies: list[DeclaredDependency]
    unreadable: list[str]


def declared_dependencies(*, project_root: Path, home: Path) -> Enumeration:
    """Every mandated dependency across all three declaration surfaces."""
    readers = (
        lambda: skills_declared_in_apm_manifest(project_root / _APM_MANIFEST),
        lambda: binaries_declared_in_pyproject(project_root / _PYPROJECT),
        lambda: integrations_declared_in_claude_settings(home.joinpath(*_CLAUDE_SETTINGS)),
    )
    dependencies: list[DeclaredDependency] = []
    unreadable: list[str] = []
    for read in readers:
        try:
            dependencies.extend(read())
        except DeclarationUnreadableError as exc:
            unreadable.append(str(exc))
    return Enumeration(dependencies=dependencies, unreadable=unreadable)
