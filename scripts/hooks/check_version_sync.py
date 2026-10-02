"""Pre-commit hook: verify version is consistent across manifests.

Checks that plugin.json, apm.yml, and pyproject.toml declare the same version.
"""

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read_pyproject_version() -> str | None:
    path = ROOT / "pyproject.toml"
    if not path.is_file():
        return None
    with path.open("rb") as f:
        data = tomllib.load(f)
    return str(data.get("project", {}).get("version", ""))


def _read_plugin_json_version() -> str | None:
    path = ROOT / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data.get("version", ""))


def _read_apm_version() -> str | None:
    path = ROOT / "apm.yml"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return ""


def main() -> int:
    # None means the repo does not ship that manifest at all — out of scope.
    # An empty string means it ships one that declares nothing, which is the
    # half-done release bump this gate exists to catch.
    declared = {
        "pyproject.toml": _read_pyproject_version(),
        ".claude-plugin/plugin.json": _read_plugin_json_version(),
        "apm.yml": _read_apm_version(),
    }
    shipped = {name: version for name, version in declared.items() if version is not None}

    undeclared = [name for name, version in shipped.items() if not version]
    if undeclared:
        print("Manifest(s) shipped without a version declaration:")
        for name in undeclared:
            print(f"  {name}")
        return 1

    if len(set(shipped.values())) > 1:
        print("Version mismatch across manifests:")
        for name, version in shipped.items():
            print(f"  {name}: {version}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
