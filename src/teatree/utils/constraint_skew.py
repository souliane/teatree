"""Installed distributions that disagree with the boot constraints file.

``deploy/entrypoint.sh`` runs this file with the tool venv's own interpreter before a
long-lived role execs ``t3``: a venv off its pins otherwise surfaces only as an import-time
``TypeError`` deep inside a dependency. Pure stdlib and never imports teatree, because the
venv it judges may be the reason teatree cannot import.

A constraint is not a requirement, so a pinned package the venv does not carry is fine.
"""

import re
import sys
from collections.abc import Mapping
from importlib.metadata import distributions
from pathlib import Path

_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;]+)")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_versions() -> dict[str, str]:
    return {_normalized(dist.metadata["Name"]): dist.version for dist in distributions() if dist.metadata["Name"]}


def constraint_skew(constraints: str, installed: Mapping[str, str]) -> list[str]:
    # A forked resolution pins one package once per marker, so any of its pins satisfies it.
    pins: dict[str, list[str]] = {}
    for line in constraints.splitlines():
        if pin := _PIN.match(line):
            pins.setdefault(_normalized(pin.group(1)), []).append(pin.group(2))
    return [
        f"{name} {installed[name]} installed, {' or '.join(pinned)} constrained"
        for name, pinned in pins.items()
        if name in installed and installed[name] not in pinned
    ]


def main(argv: list[str]) -> int:
    skew = constraint_skew(Path(argv[0]).read_text(encoding="utf-8"), installed_versions())
    sys.stdout.writelines(f"{line}\n" for line in skew)
    return 1 if skew else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
