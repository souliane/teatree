"""Classify how CI's regenerated ``dist/sbom.json`` differs from the committed one (#4907).

A Dependabot uv bump moves ``uv.lock`` and never the SBOM generated from it, so
the required ``sbom`` gate reds on every one. The Dependabot SBOM sync workflow
heals that by copying CI's own regenerated file onto the branch — but only when
this script says the two documents are equal once each component's own version
is set aside. That is the whole verdict, and it is fail-closed.

``unchanged`` means the bytes are identical, so there is nothing to sync.

``version-only`` means equal once every component's ``version`` is dropped and
that same version is masked in its own ``purl`` and ``description``, for both
``components`` and ``metadata.tools.components`` (a generator bump).

``component-set-changed`` is everything else. ``bom-ref`` is the export's line
number, so an added or removed package renumbers every ref after it; a rename, a
new key, a changed marker, a project version or a dependency-graph change all
land here too. The PR stays red for a human.

Unreadable input — not JSON, not a CycloneDX object — fails the step rather
than classifying it. Stdlib only and no ``teatree`` import: the sync job holds a
write credential and installs nothing.
"""

import argparse
import copy
import dataclasses
import json
import os
import re
import sys
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_MASK = "<version>"


class Verdict(Enum):
    UNCHANGED = "unchanged"
    VERSION_ONLY = "version-only"
    COMPONENT_SET_CHANGED = "component-set-changed"


class UnreadableBomError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Move:
    name: str
    before: str
    after: str


def parse_bom(raw: bytes) -> dict[str, Any]:
    try:
        bom = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        msg = f"not JSON: {error}"
        raise UnreadableBomError(msg) from error
    if not isinstance(bom, dict) or bom.get("bomFormat") != "CycloneDX":
        msg = "not a CycloneDX document"
        raise UnreadableBomError(msg)
    _version_bearing(bom)
    return bom


def _entries(holder: object, key: str) -> list[Any]:
    entries = holder.get(key, []) if isinstance(holder, dict) else []
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        msg = f"`{key}` is not a list of objects"
        raise UnreadableBomError(msg)
    return entries


def _version_bearing(bom: "Mapping[str, Any]") -> list[dict[str, Any]]:
    metadata = bom.get("metadata")
    tools = metadata.get("tools") if isinstance(metadata, dict) else None
    return [*_entries(bom, "components"), *_entries(tools, "components")]


def _mask_own_version(component: dict[str, Any]) -> None:
    version = component.pop("version", None)
    if not isinstance(version, str) or not version:
        return
    pinned = re.escape(version)
    if isinstance(purl := component.get("purl"), str):
        component["purl"] = re.sub(rf"@{pinned}(?=$|[?#])", f"@{_MASK}", purl)
    if isinstance(description := component.get("description"), str):
        component["description"] = re.sub(rf"=={pinned}(?=$|\s*;)", f"=={_MASK}", description)


def normalize(bom: "Mapping[str, Any]") -> dict[str, Any]:
    masked = copy.deepcopy(dict(bom))
    for component in _version_bearing(masked):
        _mask_own_version(component)
    return masked


def classify(committed: bytes, regenerated: bytes) -> Verdict:
    before, after = parse_bom(committed), parse_bom(regenerated)
    if committed == regenerated:
        return Verdict.UNCHANGED
    if normalize(before) == normalize(after):
        return Verdict.VERSION_ONLY
    return Verdict.COMPONENT_SET_CHANGED


def version_moves(committed: "Mapping[str, Any]", regenerated: "Mapping[str, Any]") -> tuple[Move, ...]:
    before = {(c.get("bom-ref"), c.get("name")): c for c in _version_bearing(committed)}
    moves = []
    for component in _version_bearing(regenerated):
        old = before.get((component.get("bom-ref"), component.get("name")))
        if old is not None and old.get("version") != component.get("version"):
            moves.append(Move(str(component.get("name")), str(old.get("version")), str(component.get("version"))))
    return tuple(moves)


def component_set_delta(
    committed: "Mapping[str, Any]", regenerated: "Mapping[str, Any]"
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The component names only the regenerated file has, then those only the committed one has."""
    before = {str(c.get("name")) for c in _entries(committed, "components")}
    after = {str(c.get("name")) for c in _entries(regenerated, "components")}
    return tuple(sorted(after - before)), tuple(sorted(before - after))


def _report(verdict: Verdict, before: "Mapping[str, Any]", after: "Mapping[str, Any]") -> None:
    print(f"SBOM drift: {verdict.value}")
    if verdict is Verdict.VERSION_ONLY:
        for move in version_moves(before, after):
            print(f"  {move.name} {move.before} -> {move.after}")
    elif verdict is Verdict.COMPONENT_SET_CHANGED:
        added, removed = component_set_delta(before, after)
        named = f" Added: {', '.join(added) or 'none'}; removed: {', '.join(removed) or 'none'}."
        print(
            "::warning::The regenerated SBOM changes more than component versions, so it is not synced "
            f"automatically.{named} Regenerate it by hand with scripts/hooks/generate_sbom.sh."
        )


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--committed", required=True, type=Path, help="dist/sbom.json as the PR head commits it")
    parser.add_argument("--regenerated", required=True, type=Path, help="dist/sbom.json as CI regenerated it")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"), help="GITHUB_OUTPUT file")
    args = parser.parse_args(argv)

    try:
        committed, regenerated = args.committed.read_bytes(), args.regenerated.read_bytes()
        verdict = classify(committed, regenerated)
    except (OSError, UnreadableBomError) as error:
        print(f"::error::Could not classify the SBOM drift: {error}. Nothing is synced.", file=sys.stderr)
        return 1

    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"verdict={verdict.value}\n")
    _report(verdict, parse_bom(committed), parse_bom(regenerated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
