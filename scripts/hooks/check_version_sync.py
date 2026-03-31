"""Pre-commit hook: verify version is consistent across manifests.

Checks that plugin.json, apm.yml, and pyproject.toml declare the same version.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read_pyproject_version() -> str:
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    return str(data.get("project", {}).get("version", ""))


def _read_plugin_json_version() -> str:
    path = ROOT / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data.get("version", ""))


def _read_apm_version() -> str:
    path = ROOT / "apm.yml"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return ""


def main() -> int:
    pyproject = _read_pyproject_version()
    plugin = _read_plugin_json_version()
    apm = _read_apm_version()

    versions = {
        "pyproject.toml": pyproject,
        ".claude-plugin/plugin.json": plugin,
        "apm.yml": apm,
    }

    present = {name: ver for name, ver in versions.items() if ver}
    unique = set(present.values())

    if len(unique) > 1:
        print("Version mismatch across manifests:")
        for name, ver in present.items():
            print(f"  {name}: {ver}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
