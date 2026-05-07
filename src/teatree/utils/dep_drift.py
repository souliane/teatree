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
