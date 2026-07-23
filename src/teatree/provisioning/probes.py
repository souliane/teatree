"""Is a declared dependency actually provisioned? One probe per kind (#3652).

A probe answers only "can this be used right now", so the answer must come from
the surface the consumer really reads: an installed ``SKILL.md`` on a skill
search dir (never the eval-fixture corpus, which no loader looks at), a binary
resolvable on PATH, a plugin whose registry entry points at a directory that
exists.
"""

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from teatree.provisioning.declared import DeclaredDependency

type BinaryResolver = Callable[[str], str | None]


def skill_is_provisioned(name: str, search_dirs: Sequence[Path]) -> bool:
    """True when *name* resolves to a loadable ``<search-dir>/<name>/SKILL.md``.

    The same enumeration the skill loader uses, so "installed" here means the
    same thing it means to an agent trying to load the skill.
    """
    return any((search_dir / name / "SKILL.md").is_file() for search_dir in search_dirs)


def binary_is_provisioned(name: str, which: BinaryResolver) -> bool:
    return which(name) is not None


def integration_is_provisioned(plugin_id: str, home: Path) -> bool:
    """True when the enabled plugin has a registry entry at a resolvable path."""
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    if not (isinstance(entries, list) and entries and isinstance(entries[0], dict)):
        return False
    install_path = entries[0].get("installPath")
    return isinstance(install_path, str) and bool(install_path) and Path(install_path).is_dir()


def unprovisioned(
    dependencies: Sequence[DeclaredDependency],
    *,
    search_dirs: Sequence[Path],
    home: Path,
    which: BinaryResolver | None = None,
) -> list[DeclaredDependency]:
    """The subset of *dependencies* that is declared but not actually provisioned."""
    resolve = shutil.which if which is None else which
    gaps: list[DeclaredDependency] = []
    for dependency in dependencies:
        if dependency.kind == "skill":
            provisioned = skill_is_provisioned(dependency.name, search_dirs)
        elif dependency.kind == "binary":
            provisioned = binary_is_provisioned(dependency.name, resolve)
        else:
            provisioned = integration_is_provisioned(dependency.name, home)
        if not provisioned:
            gaps.append(dependency)
    return gaps
