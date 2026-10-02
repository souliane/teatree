"""Classify what the weekly ``uv lock --upgrade`` moved, and gate its self-merge (#4437).

The lock-refresh workflow opens its PR with a PAT so CI fires, then enables
GitHub auto-merge: once ``test (3.13)`` is green the resolve lands with no human
step (#3245). That is the right trade for a patch inside a pinned series and the
wrong one for anything crossing a feature or major boundary — a framework
migration authorised by a lockfile nobody reads line by line.

So the workflow asks this script two questions the raw diff cannot answer: what
actually moved, and may it self-merge. The verdict is patch-only and fail-closed
— a minor, a major, or a version string this script cannot parse all refuse
auto-merge and route the PR to a human. Unreadable input is louder still: the
job reds rather than opening a PR whose provenance is unknown.

Boundaries this classifier does NOT draw: a ``0.0.z`` move reads as PATCH
because the convention leaves no axis below the patch one, and an added or
removed transitive package is reported but never blocks — the weekly whole-graph
resolve churns those routinely and neither is a version boundary.

Stdlib only, and no ``teatree`` import: the lock-refresh job runs ``uv lock
--upgrade`` and deliberately no ``uv sync``, exactly the constraint
``scripts/ci/audit_canary.py`` documents, so reaching into the package would add
a full install to a job that needs none.
"""

import argparse
import dataclasses
import os
import re
import sys
import tomllib
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_RELEASE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class Level(Enum):
    MAJOR = "major"
    MINOR = "minor"
    UNKNOWN = "unknown"
    PATCH = "patch"


_SEVERITY: dict[Level, int] = {Level.MAJOR: 0, Level.MINOR: 1, Level.UNKNOWN: 2, Level.PATCH: 3}


@dataclasses.dataclass(frozen=True)
class Move:
    name: str
    before: str
    after: str
    level: Level


@dataclasses.dataclass(frozen=True)
class LockDelta:
    upgrades: tuple[Move, ...]
    added: tuple[tuple[str, str], ...]
    removed: tuple[tuple[str, str], ...]

    @property
    def blockers(self) -> tuple[Move, ...]:
        return tuple(move for move in self.upgrades if move.level is not Level.PATCH)


def _release(version: str) -> tuple[int, int, int] | None:
    match = _RELEASE.match(version)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0)


def classify(before: str, after: str) -> Level:
    """The boundary a version move crosses, or ``UNKNOWN`` when either side is unparsable."""
    old, new = _release(before), _release(after)
    if old is None or new is None:
        return Level.UNKNOWN
    if old[0] != new[0]:
        return Level.MAJOR
    if old[1] != new[1]:
        return Level.MINOR
    return Level.PATCH


def parse_lock(text: str) -> dict[str, str]:
    """Every ``[[package]]`` in a ``uv.lock``, as ``name -> version``."""
    packages = tomllib.loads(text).get("package", [])
    return {str(entry["name"]): str(entry.get("version", "")) for entry in packages}


def compute_delta(before: "Mapping[str, str]", after: "Mapping[str, str]") -> LockDelta:
    upgrades = sorted(
        (
            Move(name, before[name], after[name], classify(before[name], after[name]))
            for name in before.keys() & after.keys()
            if before[name] != after[name]
        ),
        key=lambda move: (_SEVERITY[move.level], move.name),
    )
    return LockDelta(
        upgrades=tuple(upgrades),
        added=tuple(sorted((name, after[name]) for name in after.keys() - before.keys())),
        removed=tuple(sorted((name, before[name]) for name in before.keys() - after.keys())),
    )


def auto_merge_safe(delta: LockDelta) -> bool:
    return not delta.blockers


_PREAMBLE = (
    "Automated weekly lockfile refresh via `uv lock --upgrade`.\n\n"
    "Dependabot proposes per-package updates; it does not re-resolve the whole graph. "
    "This PR re-resolves every pin so transitive dependencies do not drift between the "
    "packages it happens to open a PR for."
)

_ENABLED = (
    "**Verdict: auto-merge enabled.** Every version move below is patch-level inside its "
    "pinned series, so this lands once `test (3.13)` is green, with no human step (#3245)."
)


def _verdict(delta: LockDelta) -> str:
    if auto_merge_safe(delta):
        return _ENABLED
    named = ", ".join(f"`{move.name}` {move.before} → {move.after} ({move.level.value})" for move in delta.blockers)
    return (
        f"**Verdict: review required — auto-merge is NOT enabled.** {len(delta.blockers)} package(s) "
        f"cross a feature/major boundary, or resolve to a version this workflow cannot classify: "
        f"{named}. A boundary crossing is a migration, not a refresh — it does not land unattended (#4437)."
    )


def _moves_section(delta: LockDelta) -> str:
    if not delta.upgrades:
        return "### Version moves\n\nNo package changed version."
    rows = [f"| `{move.name}` | {move.before} | {move.after} | {move.level.value} |" for move in delta.upgrades]
    header = ["| package | from | to | move |", "| --- | --- | --- | --- |"]
    return "### Version moves\n\n" + "\n".join([*header, *rows])


def _membership_section(delta: LockDelta) -> str:
    lines = [f"- Added `{name}` {version}" for name, version in delta.added]
    lines += [f"- Removed `{name}` {version}" for name, version in delta.removed]
    if not lines:
        return ""
    return "### Graph membership\n\n" + "\n".join(lines)


def render_body(delta: LockDelta) -> str:
    sections = [_PREAMBLE, _verdict(delta), _moves_section(delta), _membership_section(delta)]
    return "\n\n".join(section for section in sections if section) + "\n"


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 1


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path, help="uv.lock as it stood before the upgrade")
    parser.add_argument("--after", required=True, type=Path, help="uv.lock the upgrade produced")
    parser.add_argument("--body-out", required=True, type=Path, help="where to write the rendered PR body")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"), help="GITHUB_OUTPUT file")
    args = parser.parse_args(argv)

    try:
        before = parse_lock(args.before.read_text(encoding="utf-8"))
        after = parse_lock(args.after.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        return _fail(
            f"Could not classify the resolve: {error}. Refusing to open a lock-refresh PR whose "
            "moves are unknown — an unclassified resolve must never reach auto-merge (#4437)."
        )

    delta = compute_delta(before, after)
    args.body_out.write_text(render_body(delta), encoding="utf-8")
    verdict = auto_merge_safe(delta)
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"auto-merge={'true' if verdict else 'false'}\n")

    print(f"{len(delta.upgrades)} version move(s), {len(delta.added)} added, {len(delta.removed)} removed.")
    for move in delta.upgrades:
        print(f"  {move.level.value:<7} {move.name} {move.before} -> {move.after}")
    if not verdict:
        print(f"::warning::{len(delta.blockers)} package(s) cross a feature/major boundary — auto-merge withheld.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
