"""Pre-commit hook: every top-level teatree module must be declared in tach.toml.

tach silently ignores any top-level unit — directory **package** *or*
single-file **module** — that has no ``[[modules]]`` entry: its cross-layer
imports are unconstrained, so a whole subsystem can drift with a green
``tach check``. This guard fails the commit when a top-level unit under
``src/teatree/`` has no matching ``teatree.<name>`` module path, so the blind
spot that hid teatree.loop / teatree.docker (packages) — and, at single-file
granularity, teatree.visual_qa's unconstrained edge into the domain layer
(#740) — cannot recur.

Genuinely isolated leaf modules (no internal imports and no internal
importers) carry no cross-layer edge for tach to constrain; they may be
explicitly listed in ``LEAF_MODULE_ALLOWLIST`` instead of carrying an empty
``[[modules]]`` entry. The allowlist is deliberately small and explicit so
adding an internal import to a leaf is a visible, reviewed change.

Exit code 0 = every unit declared (or allowlisted), 1 = undeclared unit(s).
"""

import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "teatree"
TACH_TOML = REPO_ROOT / "tach.toml"

# Top-level single-file modules with zero internal teatree imports AND zero
# internal teatree importers — no cross-layer edge exists for tach to
# constrain, so an empty [[modules]] entry would be noise. Keep small; adding
# an internal import to one of these must move it to a real [[modules]] entry.
LEAF_MODULE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "teatree._overlay_api",
        "teatree.urls",
        "teatree.wsgi",
    }
)


def declared_module_paths() -> set[str]:
    data = tomllib.loads(TACH_TOML.read_text(encoding="utf-8"))
    return {module["path"] for module in data.get("modules", [])}


def top_level_packages() -> set[str]:
    return {
        f"teatree.{child.name}"
        for child in PACKAGE_ROOT.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }


def top_level_modules() -> set[str]:
    return {
        f"teatree.{child.stem}"
        for child in PACKAGE_ROOT.iterdir()
        if child.is_file() and child.suffix == ".py" and not child.stem.startswith("__")
    }


def top_level_units() -> set[str]:
    return top_level_packages() | top_level_modules()


def undeclared_units() -> list[str]:
    declared = declared_module_paths()
    return sorted(unit for unit in top_level_units() if unit not in declared and unit not in LEAF_MODULE_ALLOWLIST)


def main() -> int:
    missing = undeclared_units()
    if not missing:
        return 0
    print("tach.toml is missing [[modules]] entries for these units:")
    for unit in missing:
        print(f"  - {unit}")
    print("\nUndeclared top-level packages/modules are unconstrained by tach.")
    print("Add a [[modules]] entry with the correct depends_on (or, for a")
    print("genuine leaf with no internal imports/importers, add it to")
    print("LEAF_MODULE_ALLOWLIST) before committing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
