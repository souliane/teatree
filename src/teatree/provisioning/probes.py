"""Is a declared dependency actually provisioned? One probe per kind (#3652).

A probe answers only "can this be used right now", so the answer must come from
the surface the consumer really reads: an installed ``SKILL.md`` on a skill
search dir (never the eval-fixture corpus, which no loader looks at), a binary
resolvable on PATH, a plugin whose registry entry points at a directory that
exists.

A BUNDLE (#4677) is the one kind whose members are not known from the declaration:
the spec names a repo, so what it publishes is read from the fetched checkout. An
unfetched bundle is therefore not provisioned rather than vacuously satisfied —
"it publishes nothing I can see" and "nothing is missing" are different answers.
"""

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.excluded_skills import CORE_EXCLUDED_SKILLS
from teatree.provisioning.skill_bundle import parse_bundle_source, published_bundle_skills

type BinaryResolver = Callable[[str], str | None]


def skill_is_provisioned(name: str, search_dirs: Sequence[Path]) -> bool:
    """True when *name* resolves to a loadable ``<search-dir>/<name>/SKILL.md``.

    The same enumeration the skill loader uses, so "installed" here means the
    same thing it means to an agent trying to load the skill.
    """
    return any((search_dir / name / "SKILL.md").is_file() for search_dir in search_dirs)


def bundle_is_provisioned(
    dependency: DeclaredDependency,
    search_dirs: Sequence[Path],
    *,
    cache_root: Path,
    excluded: Sequence[str] = tuple(CORE_EXCLUDED_SKILLS),
) -> bool:
    """True when the bundle was fetched AND every skill it publishes is loadable."""
    source = parse_bundle_source(dependency.source)
    if source is None:
        return False
    published = published_bundle_skills(cache_root / source.cache_name)
    if not published:
        return False
    return all(skill_is_provisioned(name, search_dirs) for name in published if name not in excluded)


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
    cache_root: Path | None = None,
) -> list[DeclaredDependency]:
    """The subset of *dependencies* that is declared but not actually provisioned."""
    resolve = shutil.which if which is None else which
    gaps: list[DeclaredDependency] = []
    for dependency in dependencies:
        if dependency.kind == "skill":
            provisioned = skill_is_provisioned(dependency.name, search_dirs)
        elif dependency.kind == "bundle":
            # Resolved lazily: the default root is created on read, and a probe over a
            # manifest with no bundle must not have a filesystem side effect.
            cache = _default_cache_root() if cache_root is None else cache_root
            provisioned = bundle_is_provisioned(dependency, search_dirs, cache_root=cache)
        elif dependency.kind == "binary":
            provisioned = binary_is_provisioned(dependency.name, resolve)
        else:
            provisioned = integration_is_provisioned(dependency.name, home)
        if not provisioned:
            gaps.append(dependency)
    return gaps


def _default_cache_root() -> Path:
    from teatree.paths import get_data_dir  # noqa: PLC0415 — deferred: keeps the import graph off teatree.paths

    return get_data_dir("skill-sources")
