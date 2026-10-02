"""Canary proving the dependency-audit surface can still SEE an advisory (#4346).

The ``uv-audit`` gate reds when ``pip-audit`` reports a vulnerability. Nothing
reds when it reports *nothing* — and "no findings" is what a blind surface
looks like too: an OSV backend returning empty, a preview parser regression
(astral-sh/uv#19492 was exactly that), an over-broad allowlist, a flag rename.
A green audit is not by itself evidence the audit ran.

This canary feeds the SAME tool the gate uses a requirement pinned to a version
with permanently-known advisories and demands at least one back. Exit 0 only on
``SEEING``; ``BLIND`` and ``UNREADABLE`` both exit 1 so the two failure shapes
are distinguishable in the log.

What it does NOT cover, measured while writing it: neither ``osv`` nor ``pypi``
reports any advisory for ``django==6.0.7`` — eleven days after 6.0.8 shipped
four CVEs. So no backend could have caught #4346's miss, and this canary would
have been green throughout, correctly. It bounds the audit's green to "as
current as the advisory databases"; catching a release the databases have not
published a range for is version-currency's job, which is the dependabot
``uv``-ecosystem half of that issue.

Stdlib only, and no ``teatree`` import: the ``uv-audit`` job deliberately runs
no ``uv sync``, and making it sync to reach this file would add a full install
to every PR.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

CANARY_PACKAGE = "flask"
CANARY_REQUIREMENT = f"{CANARY_PACKAGE}==0.12.2"

# Mirrors the gate's own invocation (ci.yml `uv-audit`, .pre-commit-config.yaml
# `uv-audit`) so a blind gate is a blind canary. `--no-deps` is the one
# difference: the gate's requirements come from `uv export` and carry hashes,
# which is what `--disable-pip` otherwise requires. `--strict` is dropped
# because the verdict is read from the JSON, never from the exit code — a
# healthy run exits non-zero precisely BECAUSE it found the canary.
_AUDIT_COMMAND = (
    "uvx",
    "pip-audit",
    "--vulnerability-service",
    "osv",
    "--disable-pip",
    "--no-deps",
    "--format",
    "json",
    "-r",
)

_TIMEOUT_SECONDS = 300


class Verdict(Enum):
    SEEING = "seeing"
    BLIND = "blind"
    UNREADABLE = "unreadable"


def audit_command(requirements: Path) -> list[str]:
    return [*_AUDIT_COMMAND, str(requirements)]


def verdict_for(report: str, *, package: str = CANARY_PACKAGE) -> Verdict:
    try:
        parsed = json.loads(report)
        dependencies = parsed["dependencies"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return Verdict.UNREADABLE
    if not isinstance(dependencies, list):
        return Verdict.UNREADABLE
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return Verdict.UNREADABLE
        if str(dependency.get("name", "")).lower() == package.lower() and dependency.get("vulns"):
            return Verdict.SEEING
    return Verdict.BLIND


def run_canary(
    *,
    command: "Sequence[str] | None" = None,
    requirement: str = CANARY_REQUIREMENT,
    package: str = CANARY_PACKAGE,
) -> tuple[Verdict, str]:
    """Audit *requirement* with *command* (default: the gate's own invocation)."""
    with tempfile.TemporaryDirectory() as directory:
        requirements = Path(directory) / "requirements.canary.txt"
        requirements.write_text(f"{requirement}\n", encoding="utf-8")
        argv = [*command, str(requirements)] if command is not None else audit_command(requirements)
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Verdict.UNREADABLE, f"the audit command could not be run: {exc}"
    verdict = verdict_for(completed.stdout, package=package)
    detail = (
        completed.stdout if verdict is Verdict.SEEING else f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return verdict, detail


_FAILURE_HINT = {
    Verdict.BLIND: (
        f"the audit surface reported NO advisory for {CANARY_REQUIREMENT}, which has known ones. "
        "The gate is blind, so its green means nothing: check the OSV backend, the allowlist, and "
        "whether the tool's flags still mean what this canary assumes."
    ),
    Verdict.UNREADABLE: (
        "the audit surface produced output this canary could not parse — a crash, a network "
        "failure, or a report-format change. Either way the gate's verdict is unverified."
    ),
}


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirement", default=CANARY_REQUIREMENT, help="Pinned known-vulnerable requirement.")
    parser.add_argument("--package", default=CANARY_PACKAGE, help="Package name expected in the audit report.")
    args = parser.parse_args(argv)

    verdict, detail = run_canary(requirement=args.requirement, package=args.package)
    if verdict is Verdict.SEEING:
        print(f"audit canary OK — the audit surface reported advisories for {args.requirement}.")
        return 0
    print(f"::error::audit canary {verdict.value.upper()} — {_FAILURE_HINT[verdict]}", file=sys.stderr)
    print(detail, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
