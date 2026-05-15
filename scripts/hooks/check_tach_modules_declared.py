"""Pre-commit hook: every top-level teatree package must be declared in tach.toml.

tach silently ignores packages that have no ``[[modules]]`` entry — their
cross-layer imports are unconstrained, so a whole subsystem can drift with a
green ``tach check``. This guard fails the commit when a package under
``src/teatree/`` has no matching ``teatree.<pkg>`` module path, so the blind
spot that hid teatree.loop / teatree.docker cannot recur.

Exit code 0 = every package declared, 1 = undeclared package(s).
"""

import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "teatree"
TACH_TOML = REPO_ROOT / "tach.toml"


def declared_module_paths() -> set[str]:
    data = tomllib.loads(TACH_TOML.read_text(encoding="utf-8"))
    return {module["path"] for module in data.get("modules", [])}


def top_level_packages() -> set[str]:
    return {
        f"teatree.{child.name}"
        for child in PACKAGE_ROOT.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }


def undeclared_packages() -> list[str]:
    declared = declared_module_paths()
    return sorted(pkg for pkg in top_level_packages() if pkg not in declared)


def main() -> int:
    missing = undeclared_packages()
    if not missing:
        return 0
    print("tach.toml is missing [[modules]] entries for these packages:")
    for pkg in missing:
        print(f"  - {pkg}")
    print("\nUndeclared packages are unconstrained by tach. Add a [[modules]]")
    print("entry with the correct depends_on before committing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
