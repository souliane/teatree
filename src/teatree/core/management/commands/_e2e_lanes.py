"""The ``e2e lanes`` verb: derive ``{lane: [spec, ...]}`` from overlay seams (#3329).

``run_provenance(spec_path)`` maps a spec to its lane, but there was no inverse —
nothing emitted the split a CI matrix needs, so an overlay shipped a CLI command
purely to invert its own map. Core owns the fold here: it enumerates the
overlay's specs (:meth:`OverlayE2E.spec_paths`) and groups them by
:meth:`OverlayE2E.run_provenance`, so the matrix derives from the manifest the
overlay already registered rather than a hand-maintained spec glob.
"""

import json
from collections.abc import Callable

from teatree.core.overlay import OverlayBase

#: The lane specs land under when the overlay records no ``run_provenance`` for
#: them — surfaced explicitly rather than silently dropped, so a spec missing a
#: lane is visible in the split instead of absent from the matrix.
UNASSIGNED_LANE = "unassigned"


def lane_split(overlay: OverlayBase) -> dict[str, list[str]]:
    """Group the overlay's registered specs by their ``run_provenance`` lane.

    Returns ``{lane: [spec, ...]}`` with both lanes and specs sorted, so the
    emitted matrix is deterministic across runs.
    """
    grouped: dict[str, list[str]] = {}
    for spec in overlay.e2e.spec_paths():
        lane = overlay.e2e.run_provenance(spec) or UNASSIGNED_LANE
        grouped.setdefault(lane, []).append(spec)
    return {lane: sorted(specs) for lane, specs in sorted(grouped.items())}


def run_lanes(
    *,
    as_json: bool,
    names: bool,
    lane: str,
    overlay: OverlayBase,
    write_out: Callable[[str], None],
) -> dict[str, list[str]]:
    """Emit the lane split for ``e2e lanes``; return it for programmatic callers.

    ``lane`` filters to a single lane (empty keeps all). ``as_json`` prints the
    ``{lane: [spec, ...]}`` object (a CI matrix); ``names`` prints every spec one
    per line (a shell loop); the default prints one ``lane: spec, ...`` line per
    lane.
    """
    split = lane_split(overlay)
    if lane:
        split = {lane: split.get(lane, [])}
    if as_json:
        write_out(json.dumps(split))
    elif names:
        for specs in split.values():
            for spec in specs:
                write_out(spec)
    else:
        for lane_name, specs in split.items():
            write_out(f"{lane_name}: {', '.join(specs)}")
    return split
