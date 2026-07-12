"""Detect when an editable teatree install has drifted from ``pyproject.toml``.

Reads ``[project].dependencies`` from teatree's source tree and compares
against the dists installed in the running interpreter.  ``t3 setup`` uses
this to auto-repair stale editable installs — the catch-22 that broke
``t3 setup`` when ``tomlkit`` was first added, since the very command that
would fix the install was the one the missing dep killed.

The check intentionally has zero non-stdlib deps (``tomllib``,
``importlib.metadata``, ``json``, ``re``) so it remains usable even when
teatree's declared deps are partially missing from the venv.
"""

import json
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution, distributions
from pathlib import Path

_NORMALIZE_RE = re.compile(r"[-_.]+")
_SPEC_TERMINATORS_RE = re.compile(r"[<>=!~\s\[]")


def normalize(name: str) -> str:
    """Normalise a distribution name per PEP 503."""
    return _NORMALIZE_RE.sub("-", name).lower()


def _name_from_spec(spec: str) -> str:
    spec = spec.split(";", 1)[0].strip()
    return _SPEC_TERMINATORS_RE.split(spec, maxsplit=1)[0].strip()


def declared_dependency_names(pyproject_path: Path) -> set[str]:
    """Return the normalized names from ``[project].dependencies``."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", []) or []
    return {normalize(_name_from_spec(spec)) for spec in deps if spec.strip()}


def installed_distribution_names() -> set[str]:
    """Return the set of normalized dist names visible to this interpreter."""
    return {normalize(dist.name) for dist in distributions()}


def find_missing_dependencies(pyproject_path: Path) -> list[str]:
    """Return declared deps not installed in this interpreter, sorted."""
    return sorted(declared_dependency_names(pyproject_path) - installed_distribution_names())


def running_prefix() -> Path:
    """Return ``sys.prefix`` of the interpreter actually executing ``t3``.

    The dep-drift check must detect and repair *this* environment — the one
    whose ``importlib.metadata`` is read by :func:`find_missing_dependencies`
    — not whatever environment ``uv tool`` happens to manage.  When the
    running ``t3`` is a plain editable install (``pip install -e .`` into a
    pyenv/virtualenv site-packages) it is a *different* env than the
    ``uv tool``-managed one, so a ``uv tool install`` repair would never
    touch the running interpreter.
    """
    return Path(sys.prefix)


def running_python() -> Path:
    """Return the interpreter executing ``t3`` (``sys.executable``)."""
    return Path(sys.executable)


def running_env_is_uv_tool() -> bool:
    """``True`` iff the running interpreter lives under ``uv``'s tool dir.

    A ``uv tool install``-managed teatree has its ``sys.prefix`` nested under
    ``~/.local/share/uv/tools`` (or ``$UV_TOOL_DIR``).  Detection is purely
    path-based so it stays usable with no non-stdlib deps even when teatree's
    declared deps are partially missing.  When this returns ``False`` the
    running env must be repaired in place (install into ``sys.prefix``),
    never via ``uv tool install`` (which targets a foreign env).
    """
    import os  # noqa: PLC0415 — deferred: loaded only on this code path

    prefix = running_prefix().resolve()
    candidates: list[Path] = []
    env_dir = os.environ.get("UV_TOOL_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates.append(Path.home() / ".local" / "share" / "uv" / "tools")
    for tool_dir in candidates:
        try:
            resolved = tool_dir.expanduser().resolve()
        except OSError:
            continue
        if prefix == resolved or resolved in prefix.parents:
            return True
    return False


def editable_source_path() -> Path | None:
    """Return the editable source path of the running teatree, or ``None``.

    Reads PEP 660 ``direct_url.json`` from teatree's dist-info.  Returns
    ``None`` when teatree is installed non-editable (PyPI/wheel), when the
    metadata is missing/unparsable, or when the recorded URL is not a
    ``file://`` URL.
    """
    try:
        dist = distribution("teatree")
    except PackageNotFoundError:
        return None
    direct_url = dist.read_text("direct_url.json")
    if not direct_url:
        return None
    try:
        data = json.loads(direct_url)
    except json.JSONDecodeError:
        return None
    if not data.get("dir_info", {}).get("editable"):
        return None
    url = data.get("url", "")
    if not url.startswith("file://"):
        return None
    return Path(url.removeprefix("file://"))
